"""toolwatch 판정 로직 (S1~S5, E1).

디바운스/세션/미반납/반납대기 판정 순수 함수와 YOLO 추론을 모은다. 순수 함수는
DB/네트워크 I/O가 없어 라우트 없이도 _selfcheck에서 그대로 검증 가능하다
(계획서 §3.2/§3.3).
"""
import io
from collections import Counter
from datetime import datetime

from PIL import Image

import db
from state import CONFIG, ROOT_DIR, SNAPSHOT_DIR

_model = None  # ultralytics 모델 캐시, 최초 추론 시점까지 지연 로딩


def judge_tools(detected_counts, registered_stock, debounce_frames, debounce_state):
    """순수 함수: 검출 개수와 이전 디바운스 상태 → (새 디바운스 상태, 확정 이벤트 목록).

    공구별로 대여 중 개수를 `등록 재고 - 검출 개수`로 계산하고, 그 값이 N프레임
    연속 동일하게 관찰되어야만 직전 확정값과의 차이만큼 OUT/IN 이벤트를 낸다
    (계획서 §3.2). I/O 없음 — DB 기록·스냅샷 저장은 호출부에서 처리한다.
    """
    new_state = {}
    events = []
    for tool, registered in registered_stock.items():
        detected = detected_counts.get(tool, 0)
        rented_now = max(0, registered - detected)  # 오검출로 등록 수보다 많이 잡혀도 0 아래로는 안 내려감

        prev = debounce_state.get(tool, {"confirmed": 0, "candidate": 0, "streak": 0})
        streak = prev["streak"] + 1 if rented_now == prev["candidate"] else 1
        confirmed = prev["confirmed"]

        if confirmed is None:
            # 재고 변경 직후 재기준선 대기 중 — 이벤트 없이 streak 도달 시점에 조용히 채택
            if streak >= debounce_frames:
                confirmed = rented_now
        elif streak >= debounce_frames and rented_now != confirmed:
            delta = rented_now - confirmed
            event_type = "OUT" if delta > 0 else "IN"
            events.extend({"tool": tool, "type": event_type} for _ in range(abs(delta)))
            confirmed = rented_now

        new_state[tool] = {"confirmed": confirmed, "candidate": rented_now, "streak": streak}

    return new_state, events


def resolve_session(session_, uid, now, session_sec):
    """순수 함수: RFID 세션 갱신.

    새 UID 태그가 오면 기존 세션 유효 여부와 무관하게 즉시 종료하고 새 세션을 연다
    (마지막 태그 우선, 계획서 §3.3). 태그가 없으면 기존 세션이 아직 유효한지만 본다.
    """
    if uid:
        return {"uid": uid, "expires_at": now + session_sec}
    if session_ and now < session_["expires_at"]:
        return session_
    return None


def attribute_out(session_, uid_names):
    """순수 함수: 유효한 세션이면 (uid, 이름, unauth=uid_names 미등록 여부), 세션 없으면(=미확인 반출) (None, "", True).
    등록되지 않은 UID로 태깅된 경우도 신원을 확인할 수 없으므로 미확인 반출로 취급한다 (F7)."""
    if session_ is None:
        return None, "", True
    uid = session_["uid"]
    name = uid_names.get(uid, "")
    return uid, name, uid not in uid_names


def check_overdue(rented_by_tool, overdue_sec, now):
    """순수 함수: 대여 중 항목들에서 이번에 새로 미반납 초과로 전환된 것만 골라낸다.

    이미 기록한 항목은 overdue_logged로 표시해 다음 프레임에서 중복 기록하지 않는다
    (계획서 §3.2 미반납 경고, DB 기록은 초과 판정 순간 1회만).
    item에 due_at(epoch, 학생이 E2에서 설정한 반납 기한)이 있으면 그 시각 기준으로,
    없으면 기존 overdue_sec 기준으로 판정한다 (확장계획.md §E2).
    """
    new_rented = {}
    events = []
    for tool, items in rented_by_tool.items():
        new_items = []
        for item in items:
            item = dict(item)
            due_at = item.get("due_at")
            is_over = (now > due_at) if due_at is not None else (now - item["out_time"] > overdue_sec)
            if not item["cleared"] and not item["overdue_logged"] and is_over:
                # loan_id는 DB 반영(UPDATE)을 위해 호출부로 전달 — 순수 함수 자체는 I/O 없음 유지
                events.append({"tool": tool, "uid": item["uid"], "name": item["name"], "loan_id": item.get("loan_id")})
                item["overdue_logged"] = True
            new_items.append(item)
        if new_items:  # F9: 반납으로 빈 리스트가 된 공구는 잔류시키지 않음
            new_rented[tool] = new_items
    return new_rented, events


def expire_returns(returns, now):
    """순수 함수: 반납 대기(state["returns"]) 중 만료된 항목을 걸러낸다.

    태그(tagged)했는데 60초 안에 IN이 확정되지 않은 경우만 경보 대상(RETURN_FAIL) —
    태그 없이 그냥 시간이 지난 경우는 조용히 삭제한다.
    반환: (남은 returns, 경보 여부, 실패 목록[{"uid","tool","loan_id"}])
    """
    remaining = {}
    failures = []
    for uid, entry in returns.items():
        if entry["expires_at"] < now:
            if entry["tagged"]:
                failures.append({"uid": uid, "tool": entry["tool"], "loan_id": entry["loan_id"]})
        else:
            remaining[uid] = entry
    return remaining, bool(failures), failures


def decide_response(tool_status, rented_items=()):
    """light/buzzer 판정을 모아두는 단일 지점 (우선순위 적 > 황 > 녹, 계획서 §3.2).

    red: 해제되지 않은 미반납 초과 또는 미확인 반출이 하나라도 있을 때.
    buzzer: 미확인 반출이면 unauth, 아니면 미반납 초과면 overdue, 아니면 off (둘 다면 unauth 우선).
    overdue/unauth 판정은 check_overdue/attribute_out이 기록해 둔 플래그를 그대로 쓴다 (F8: 재계산 금지, 단일 판정 지점).
    """
    active = [r for r in rented_items if not r["cleared"]]
    has_unauth = any(r["unauth"] for r in active)
    has_overdue = any(r["overdue_logged"] for r in active)

    if has_unauth or has_overdue:
        light = "red"
    elif any(info["rented"] > 0 for info in tool_status.values()):
        light = "yellow"
    else:
        light = "green"

    buzzer = "unauth" if has_unauth else ("overdue" if has_overdue else "off")
    return {"light": light, "buzzer": buzzer}


def get_model():
    global _model
    if _model is None:
        from ultralytics import YOLO  # 무거운 의존성이라 최초 추론 시점까지 지연 로딩

        model_path = ROOT_DIR / CONFIG["model_path"]
        if not model_path.exists():
            model_path = CONFIG["fallback_model"]  # 학습된 모델이 없을 때 개발용 사전학습 모델로 폴백
        _model = YOLO(str(model_path))
    return _model


def detect_tools(frame_bytes):
    """프레임 바이트 → ROI 크롭 → YOLO 추론 → 공구명별 검출 개수 dict."""
    image = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
    roi = CONFIG.get("roi")
    if roi:
        image = image.crop((roi["x1"], roi["y1"], roi["x2"], roi["y2"]))

    results = get_model()(image, conf=CONFIG["confidence_threshold"], verbose=False)
    r = results[0]
    return dict(Counter(r.names[int(c)] for c in r.boxes.cls.tolist()))


def save_snapshot(tool, frame_bytes):
    """반출 확정 순간의 프레임(이미 받은 바이트)을 그대로 저장 — 재촬영·재추론 없음."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{tool}.jpg"
    (SNAPSHOT_DIR / filename).write_bytes(frame_bytes)
    return f"{SNAPSHOT_DIR.relative_to(ROOT_DIR).as_posix()}/{filename}"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def restore_rented_state(conn):
    """서버 기동 시 반납되지 않은 loan으로 state["rented"] 큐를 복원한다 (재시작 내성, 확장계획.md §E1)."""
    uid_names = db.list_users(conn)
    rented = {}
    for row in db.get_open_loans(conn):
        out_time = datetime.strptime(row["out_at"], "%Y-%m-%d %H:%M:%S").timestamp()
        due_at = (
            datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M:%S").timestamp()
            if row["due_at"] else None
        )
        uid = row["uid"] or ""
        item = {
            "uid": uid, "name": uid_names.get(uid, ""), "out_time": out_time, "due_at": due_at,
            "cleared": bool(row["cleared"]), "overdue_logged": bool(row["overdue_logged"]),
            "unauth": bool(row["unauth"]), "loan_id": row["id"],
        }
        rented.setdefault(row["tool"], []).append(item)
    return rented
