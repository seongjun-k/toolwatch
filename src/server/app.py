"""toolwatch 서버 (S1~S5, E1).

/frame 수신 → YOLO 추론(ROI 크롭) → 등록 재고 대비 검출 개수 대조 → 디바운스로
OUT/IN 확정 → RFID 세션 귀속 → 미반납/미확인 경고 → DB(events/loans) 기록 + 반출 스냅샷 저장.
`/` 는 조회 겸 제어 대시보드(고정 비밀번호 로그인).
"""
import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

import db
import push

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
DB_PATH = BASE_DIR / "toolwatch.db"  # selfcheck에서는 임시 파일 경로로 교체(구현계획.md §8)

with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)

# config의 파일 경로는 저장소 루트 기준 상대경로 — 실행 위치(cwd)와 무관하게 동작하도록 절대경로로 정규화
for _key in ("vapid_private_key_file",):
    if CONFIG.get(_key):
        CONFIG[_key] = str(ROOT_DIR / CONFIG[_key])

app = Flask(__name__)
app.secret_key = CONFIG["secret_key"]

# 상태는 전부 메모리 보관 (DB 없음, 재시작 시 초기화되어도 무방 — 구현계획.md §4)
# rented: {공구명: [{"uid","name","out_time","cleared","overdue_logged"}, ...]} (같은 공구 여러 개는 큐로, IN 시 오래된 것부터 해제)
state = {
    "latest_frame": None,
    "tool_status": {},
    "last_updated": None,
    "rented": {},
    "rfid_session": None,  # {"uid":..., "expires_at":...} or None
    "reservations": {},  # {uid: {"tool","due_epoch","due_str","expires_at"}} — /me/reserve 60초 예약
    "returns": {},  # {uid: {"tool","loan_id","expires_at","tagged"}} — /me/return 60초 반납 대기
    "return_alarm_until": 0,  # epoch, 이 시각까지 반납 실패 경보(적색+unauth 부저) 강제
}
_debounce_state = {}
_session_at_streak = {}  # 공구별 스트릭 시작 시점의 rfid_session 스냅샷 (F6: 확정 시점이 아닌 시작 시점 세션으로 귀속)
_frame_at_streak = {}  # 공구별 스트릭 시작 시점의 frame_bytes 스냅샷 (반출 확정 시점엔 손이 이미 사라져 증거로 부적합)
_model = None  # ultralytics 모델 캐시, 최초 추론 시점까지 지연 로딩
# ponytail: 전역 락 — 요청 빈도(3초 주기+대시보드)가 낮아 충분, 병목 시 세분화
_state_lock = threading.Lock()


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


def save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


def flush_pushes(pending):
    """모아둔 [(uid, body)] 푸시를 발송한다 — send_push는 네트워크 I/O라 _state_lock을 잡은 채
    부르면 /frame 처리가 푸시 서버 지연만큼 블로킹되므로 반드시 락 밖에서 호출한다."""
    if not pending:
        return
    conn = db.get_conn(DB_PATH)
    try:
        for push_uid, body in pending:
            try:  # 푸시는 부가 기능 — 실패해도 판정 흐름에 영향 금지 (확장계획.md §E3)
                push.send_push(conn, push_uid, "toolwatch", body, CONFIG)
            except Exception as e:
                print(f"[push] 알림 실패: {e}")
    finally:
        conn.close()


# ponytail: 메모리 카운터 — 재시작 시 초기화, 프록시 뒤 XFF 신뢰. 실서비스면 flask-limiter
_login_fails = {}  # ip -> {"count": int, "until": epoch} — 5회 실패 시 30초 잠금
_login_fails_lock = threading.Lock()


def _client_ip():
    # XFF의 첫 값은 클라이언트가 위조 가능 — 마지막 값(직전 프록시=Funnel이 붙인 실제 접속 IP)만 신뢰
    # (위조 허용 시 로그인 잠금 우회 + _login_fails 무한 증식)
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[-1].strip()


def login_blocked(ip):
    with _login_fails_lock:
        entry = _login_fails.get(ip)
        if entry is None:
            return False
        if entry["count"] < 5:
            return False
        if time.time() >= entry["until"]:
            del _login_fails[ip]
            return False
        return True


def record_login_result(ip, ok):
    with _login_fails_lock:
        if ok:
            _login_fails.pop(ip, None)
            return
        entry = _login_fails.setdefault(ip, {"count": 0, "until": 0})
        entry["count"] += 1
        if entry["count"] >= 5:
            entry["until"] = time.time() + 30


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/frame", methods=["POST"])
def receive_frame():
    # 외부망 노출(터널) 시 무인증 /frame으로 가짜 프레임을 주입할 수 있으므로 Pi와 공유하는 토큰으로 차단
    frame_token = CONFIG.get("frame_token")
    if frame_token and request.headers.get("X-Frame-Token") != frame_token:
        return jsonify({"error": "unauthorized"}), 401
    image_file = request.files.get("image")
    if image_file is None:
        return jsonify({"error": "image required"}), 400
    frame_bytes = image_file.read()
    uid = request.form.get("uid", "")
    pending_pushes = []  # (uid, body) — 발송은 락 해제 후 flush_pushes에서

    with _state_lock:
        now = time.time()
        conn = db.get_conn(DB_PATH)
        try:
            state["rfid_session"] = resolve_session(state["rfid_session"], uid, now, CONFIG["rfid_session_sec"])

            # 반납 대기 중인 학생이 이번 프레임에 카드를 태그했으면 표시 (IN 미확정 시 경보 판정용)
            if uid and uid in state["returns"] and state["returns"][uid]["expires_at"] > now:
                state["returns"][uid]["tagged"] = True

            detected_counts = detect_tools(frame_bytes)

            global _debounce_state
            _debounce_state, events = judge_tools(
                detected_counts, CONFIG["registered_stock"], CONFIG["debounce_frames"], _debounce_state
            )

            # F6: 새로 스트릭이 시작된(=1) 공구는 이번 시점의 세션을 확정 귀속용으로 스냅샷
            # (디바운스 지연 약 9초 중 다른 사람이 태깅하면 확정 시점 세션으로는 오귀속되므로 시작 시점 세션 사용)
            for tool, d_state in _debounce_state.items():
                if d_state["streak"] == 1:
                    _session_at_streak[tool] = state["rfid_session"]
                    _frame_at_streak[tool] = frame_bytes

            uid_names = db.list_users(conn)
            for event in events:
                tool = event["tool"]
                if event["type"] == "OUT":
                    # 확정 프레임 대신 감소가 처음 관측된(streak=1) 프레임을 증거로 저장 — 확정 시점엔 손이 이미 사라져 있음
                    snapshot_path = save_snapshot(tool, _frame_at_streak.get(tool, frame_bytes))
                    attributed_uid, name, unauth = attribute_out(_session_at_streak.get(tool), uid_names)

                    # 예약 대여: 60초 내 본인 카드로 예약한 공구를 반출하면 반납 기한을 loan에 바로 반영
                    reservation = state["reservations"].get(attributed_uid) if attributed_uid else None
                    due_str = None
                    due_epoch = None
                    if reservation and reservation["expires_at"] > now and reservation["tool"] == tool:
                        due_str = reservation["due_str"]
                        due_epoch = reservation["due_epoch"]
                        state["reservations"].pop(attributed_uid, None)

                    loan_id = db.insert_loan(conn, tool, attributed_uid or "", now_str(), unauth, snapshot_path, due_at=due_str)
                    db.add_event(conn, "OUT", tool, uid=attributed_uid or "", loan_id=loan_id, snapshot_path=snapshot_path)
                    if unauth:
                        # OUT 행은 그대로 두고 미확인 반출만 별도 행으로 추가 기록 (대시보드 이력에서 구분용)
                        db.add_event(conn, "UNAUTH", tool, uid=attributed_uid or "", loan_id=loan_id, snapshot_path=snapshot_path)
                    if attributed_uid:
                        tool_label = CONFIG.get("tool_labels", {}).get(tool, tool)
                        pending_pushes.append((attributed_uid, f"{tool_label} 대여 처리됨"))
                    state["rented"].setdefault(tool, []).append({
                        "uid": attributed_uid or "", "name": name, "out_time": now, "due_at": due_epoch,
                        "cleared": False, "overdue_logged": False, "unauth": unauth, "loan_id": loan_id,
                    })
                else:  # IN — 같은 공구 여러 개는 큐라 오래된 것부터 반납 처리
                    queue = state["rented"].get(tool, [])
                    item = queue.pop(0) if queue else {"uid": "", "name": "", "loan_id": None}
                    if not queue:
                        state["rented"].pop(tool, None)  # F9: 비면 잔류시키지 않음
                    if item.get("loan_id") is not None:
                        db.close_loan(conn, item["loan_id"], now_str())
                    db.add_event(conn, "IN", tool, uid=item.get("uid", ""), loan_id=item.get("loan_id"))
                    if item.get("uid"):
                        tool_label = CONFIG.get("tool_labels", {}).get(tool, tool)
                        pending_pushes.append((item["uid"], f"{tool_label} 반납 완료"))
                    # 정상 반납: 이 loan_id(또는 tool 일치)로 대기 중이던 반납 예약을 지운다
                    return_uid = next(
                        (
                            u for u, r in state["returns"].items()
                            if r["loan_id"] == item.get("loan_id") or r["tool"] == tool
                        ),
                        None,
                    )
                    if return_uid is not None:
                        state["returns"].pop(return_uid, None)

            state["rented"], overdue_events = check_overdue(state["rented"], CONFIG["overdue_sec"], now)
            for ev in overdue_events:
                if ev["loan_id"] is not None:
                    db.mark_overdue(conn, ev["loan_id"])
                db.add_event(conn, "OVERDUE", ev["tool"], uid=ev["uid"], loan_id=ev["loan_id"])

            # 반납 대기 만료 스캔: 태그했는데 IN 미확정이면 30초 경보 + RETURN_FAIL 1회 기록
            state["returns"], alarm, return_failures = expire_returns(state["returns"], now)
            if alarm:
                state["return_alarm_until"] = now + 30
            for fail in return_failures:
                db.add_event(conn, "RETURN_FAIL", fail["tool"], uid=fail["uid"], loan_id=fail["loan_id"])
        finally:
            conn.close()

        state["latest_frame"] = frame_bytes
        state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["tool_status"] = {
            tool: {
                "registered": registered,
                "detected": detected_counts.get(tool, 0),
                # 재기준선 대기 중(confirmed=None)엔 0 대신 실제 rented 큐 길이로 표시 — 재고 변경 직후
                # 수 초간 경광등이 실제로는 대여 중인데 정상(녹색)으로 잘못 보이는 것을 막는다
                "rented": (
                    _debounce_state[tool]["confirmed"]
                    if _debounce_state[tool]["confirmed"] is not None
                    else len(state["rented"].get(tool, []))
                ),
            }
            for tool, registered in CONFIG["registered_stock"].items()
        }

        rented_items = [item for items in state["rented"].values() for item in items]
        response = decide_response(state["tool_status"], rented_items)
        # 반납 실패 경보는 rented 플래그가 아닌 시한부 상태(return_alarm_until)라 decide_response의
        # 단일 판정 지점 밖에서 호출부가 덮어쓴다 (함수 시그니처는 그대로 유지)
        if now < state["return_alarm_until"]:
            response["light"] = "red"
            response["buzzer"] = "unauth"
        # 디바운스 진행 중(변화 관측 중)인 공구가 있으면 다음 캡처를 빠르게, 없으면 기본 주기 유지 지시
        in_progress = any(
            d["candidate"] != d["confirmed"] for d in _debounce_state.values()
        )
        response["interval"] = CONFIG.get("fast_interval_sec", 0.5) if in_progress else CONFIG.get("capture_interval_sec", 3)

    flush_pushes(pending_pushes)
    return jsonify(response)


@app.route("/")
@login_required
def dashboard():
    with _state_lock:
        now = time.time()
        rented_view = {
            tool: [
                {
                    "uid": item["uid"] or "미확인",
                    "name": item["name"] or "-",
                    "elapsed_sec": int(now - item["out_time"]),
                    "overdue": not item["cleared"] and item["overdue_logged"],
                    "unauth": not item["cleared"] and item["unauth"],
                }
                for item in items
            ]
            for tool, items in state["rented"].items()
        }
        latest_frame_b64 = base64.b64encode(state["latest_frame"]).decode() if state["latest_frame"] else None
        conn = db.get_conn(DB_PATH)
        try:
            events = db.get_recent_events(conn)
            users_full = db.list_users_full(conn)
        finally:
            conn.close()
        return render_template(
            "dashboard.html",
            logged_in=True,
            tool_status=state["tool_status"],
            rented=rented_view,
            events=events,
            latest_frame_b64=latest_frame_b64,
            last_updated=state["last_updated"],
            config=CONFIG,
            users_full=users_full,
        )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = _client_ip()
        if login_blocked(ip):
            error = "시도가 너무 많습니다. 잠시 후 다시 시도하세요"
        elif request.form.get("password") == CONFIG["hmi_password"]:
            record_login_result(ip, True)
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        else:
            record_login_result(ip, False)
            error = "비밀번호가 틀렸습니다"
    return render_template("dashboard.html", logged_in=False, error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))


@app.route("/control", methods=["POST"])
@login_required
def control():
    global _debounce_state
    if request.form.get("action") == "push_test":
        # 상태를 건드리지 않고 네트워크 I/O만 하므로 _state_lock 밖에서 처리 (푸시 지연이 /frame을 막지 않게)
        uid = request.form.get("uid", "")
        conn = db.get_conn(DB_PATH)
        try:
            subs = db.get_subscriptions(conn, uid)
            if subs:
                push.send_push(conn, uid, "toolwatch", "알림 테스트입니다", CONFIG)
                flash(f"테스트 알림 발송 (구독 {len(subs)}건)")
            else:
                flash("이 사용자는 알림 구독이 없습니다 (학생 페이지에서 '알림 켜기' 필요)")
        finally:
            conn.close()
        return redirect(url_for("dashboard"))
    with _state_lock:
        action = request.form.get("action")
        conn = db.get_conn(DB_PATH)
        try:
            if action == "stock":
                for tool in CONFIG["registered_stock"]:
                    value = request.form.get(f"stock_{tool}")
                    if value is not None and value.isdigit() and int(value) != CONFIG["registered_stock"][tool]:
                        CONFIG["registered_stock"][tool] = int(value)
                        # F2: 재고 수량이 실제로 바뀐 공구는 디바운스를 재기준선 대기 상태로 리셋 (유령 이벤트 방지)
                        _debounce_state[tool] = {"confirmed": None, "candidate": 0, "streak": 0}
                save_config()
            elif action == "overdue_sec":
                value = request.form.get("overdue_sec", "")
                if value.isdigit():
                    CONFIG["overdue_sec"] = int(value)
                save_config()
            elif action == "clear_warnings":
                # 미확인/미반납 경고만 끈다 — 대여 자체는 유지, 반납은 여전히 IN 검출로만 확정
                for items in state["rented"].values():
                    for item in items:
                        if item["unauth"] or item["overdue_logged"]:
                            item["cleared"] = True
                            if item.get("loan_id") is not None:
                                db.clear_loan_warning(conn, item["loan_id"])
            elif action == "uid_add":
                uid = request.form.get("uid", "").strip()
                name = request.form.get("name", "").strip()
                student_id = request.form.get("student_id", "").strip()
                if uid and name:
                    try:
                        db.add_user(conn, uid, name, student_id or None)
                    except sqlite3.IntegrityError:
                        # 학번 유니크 인덱스 위반 — 500 대신 안내 문구로
                        flash("이미 사용 중인 학번입니다")
            elif action == "uid_delete":
                db.delete_user(conn, request.form.get("uid", ""))
            elif action == "pw_reset":
                # 비밀번호 분실 대응: 해시를 비워 두면 학생이 "계정 생성"으로 다시 설정한다
                db.set_user_password(conn, request.form.get("uid", ""), None)
                flash("비밀번호를 초기화했습니다 — 학생이 '계정 생성'으로 재설정하면 됩니다")
            elif action == "uid_assign":
                old_uid = request.form.get("old_uid", "")
                new_uid = request.form.get("new_uid", "").strip()
                if new_uid:
                    if db.get_user(conn, new_uid):
                        flash("이미 등록된 카드입니다")
                    else:
                        db.assign_uid(conn, old_uid, new_uid)
        finally:
            conn.close()
    return redirect(url_for("dashboard"))


@app.route("/snapshots/<path:filename>")
@login_required  # 반출 증거 사진이라 파일명이 추측 가능해도 로그인 없이는 열람 불가해야 한다
def snapshot_file(filename):
    return send_from_directory(SNAPSHOT_DIR, filename)


def student_login_required(view):
    """관리자 login_required와 별개 — session["student_uid"] 기준 (확장계획.md §E2)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("student_uid"):
            return redirect(url_for("student_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/me", methods=["GET", "POST"])
def student_login():
    error = None
    if request.method == "POST":
        if request.form.get("action") == "logout":
            session.pop("student_uid", None)
            return redirect(url_for("student_login"))
        ip = _client_ip()
        if login_blocked(ip):
            error = "시도가 너무 많습니다. 잠시 후 다시 시도하세요"
            return render_template("student.html", page="login", error=error)
        student_id = request.form.get("student_id", "").strip()
        password = request.form.get("password", "")
        conn = db.get_conn(DB_PATH)
        try:
            user = db.get_user_by_student(conn, student_id)
        finally:
            conn.close()
        if user and user["password_hash"] and check_password_hash(user["password_hash"], password):
            record_login_result(ip, True)
            session["student_uid"] = user["uid"]
            return redirect(url_for("student_loans"))
        record_login_result(ip, False)
        if user and not user["password_hash"]:
            error = "계정이 없습니다. 먼저 계정 생성을 해주세요"
        else:
            error = "등록된 학번/비밀번호와 일치하지 않습니다"
    elif session.get("student_uid"):
        return redirect(url_for("student_loans"))
    return render_template("student.html", page="login", error=error)


@app.route("/me/signup", methods=["POST"])
def student_signup():
    ip = _client_ip()
    if login_blocked(ip):
        error = "시도가 너무 많습니다. 잠시 후 다시 시도하세요"
        return render_template("student.html", page="login", error=error)
    student_id = request.form.get("student_id", "").strip()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    error = None
    if not (4 <= len(password) <= 8):
        error = "비밀번호는 4~8자리로 해주세요"
    else:
        conn = db.get_conn(DB_PATH)
        try:
            user = db.get_user_by_student(conn, student_id)
            if not user:
                # 관리자 미등록 학번 — pending 사용자로 자가 가입 (카드는 관리자가 나중에 등록)
                uid = db.create_pending_user(conn, student_id, name, generate_password_hash(password))
                record_login_result(ip, True)
                session["student_uid"] = uid
                return redirect(url_for("student_loans"))
            elif user["name"] != name:
                error = "관리자에게 등록된 학번/이름이 아닙니다"
            elif user["password_hash"]:
                error = "이미 계정이 있습니다"
            else:
                db.set_user_password(conn, user["uid"], generate_password_hash(password))
                record_login_result(ip, True)
                session["student_uid"] = user["uid"]
                return redirect(url_for("student_loans"))
        finally:
            conn.close()
    record_login_result(ip, False)
    return render_template("student.html", page="login", error=error)


@app.route("/me/loans")
@student_login_required
def student_loans():
    uid = session["student_uid"]
    conn = db.get_conn(DB_PATH)
    try:
        # 관리자가 카드를 등록하면 uid가 pending:<학번> → 실제 카드 UID로 바뀜 — 세션도 따라 갱신
        if uid.startswith("pending:"):
            user = db.get_user_by_student(conn, uid.split(":", 1)[1])
            if user and user["uid"] != uid:
                uid = session["student_uid"] = user["uid"]
        uid_names = db.list_users(conn)
        rows = db.get_loans_by_uid(conn, uid)
    finally:
        conn.close()

    with _state_lock:
        now = time.time()
        tools = [
            {
                "key": tool, "label": CONFIG.get("tool_labels", {}).get(tool, tool),
                "available": max(0, registered - len(state["rented"].get(tool, []))),
            }
            for tool, registered in CONFIG["registered_stock"].items()
        ]
        res = state["reservations"].get(uid)
        if res and res["expires_at"] > now:
            reservation = {
                "tool_label": CONFIG.get("tool_labels", {}).get(res["tool"], res["tool"]),
                "remaining_sec": int(res["expires_at"] - now),
            }
        else:
            reservation = None
            state["reservations"].pop(uid, None)  # 만료된 예약은 조회 시점에 정리

        ret = state["returns"].get(uid)
        if ret and ret["expires_at"] > now:
            my_return = {
                "tool_label": CONFIG.get("tool_labels", {}).get(ret["tool"], ret["tool"]),
                "remaining_sec": int(ret["expires_at"] - now),
                "tagged": ret["tagged"],
            }
            my_return_loan_id = ret["loan_id"]
        else:
            my_return = None
            my_return_loan_id = None  # 만료 시 표시만 안 함 — 경보 판정은 /frame 쪽 스캔이 담당

    open_loans = []
    returned_loans = []
    for row in rows:
        if row["returned_at"] is None:
            due_at = row["due_at"]
            open_loans.append({
                "id": row["id"],
                "tool": row["tool"],
                "out_at": row["out_at"],
                "due_at": due_at,
                # 카운트다운 JS의 data-due 값 (YYYY-MM-DDTHH:MM, 로컬 시간)
                "due_at_input": due_at.replace(" ", "T")[:16] if due_at else "",
                # F8과 동일하게 판정은 check_overdue가 기록해 둔 플래그만 사용 (재계산 금지)
                "overdue": bool(row["overdue_logged"]) and not bool(row["cleared"]),
            })
        else:
            returned_loans.append({
                "id": row["id"], "tool": row["tool"],
                "out_at": row["out_at"], "returned_at": row["returned_at"],
            })

    return render_template(
        "student.html", page="loans", name=uid_names.get(uid, ""),
        is_pending=uid.startswith("pending:"),
        open_loans=open_loans, returned_loans=returned_loans[:5],
        tool_labels=CONFIG.get("tool_labels", {}),
        tools=tools, reservation=reservation,
        my_return=my_return, my_return_loan_id=my_return_loan_id,
    )


@app.route("/me/reserve", methods=["POST"])
@student_login_required
def student_reserve():
    uid = session["student_uid"]
    tool = request.form.get("tool", "")
    due_day_raw = request.form.get("due_day", "")  # 0=오늘 ~ 3=3일 후 (탭 화이트리스트)
    due_time_raw = request.form.get("due_time", "")  # HH:MM — 시각은 상세 지정 허용
    if tool not in CONFIG["registered_stock"] or due_day_raw not in ("0", "1", "2", "3"):
        return redirect(url_for("student_loans"))
    try:
        hh, mm = (int(x) for x in due_time_raw.split(":"))
        due_dt = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=int(due_day_raw))
    except ValueError:
        return redirect(url_for("student_loans"))
    due_epoch = due_dt.timestamp()
    if due_epoch <= time.time():  # 오늘 탭에 이미 지난 시각 조합 거부
        flash("과거로 시간여행을 하셨습니다. 반납 기한은 미래로 잡아주세요.")
        return redirect(url_for("student_loans"))

    with _state_lock:
        state["reservations"][uid] = {
            "tool": tool,
            "due_epoch": due_epoch,
            "due_str": datetime.fromtimestamp(due_epoch).strftime("%Y-%m-%d %H:%M:%S"),
            "expires_at": time.time() + 60,
        }
    return redirect(url_for("student_loans"))


@app.route("/me/reserve/cancel", methods=["POST"])
@student_login_required
def student_reserve_cancel():
    uid = session["student_uid"]
    with _state_lock:
        state["reservations"].pop(uid, None)
    return redirect(url_for("student_loans"))


@app.route("/me/return", methods=["POST"])
@student_login_required
def student_return():
    uid = session["student_uid"]
    loan_id_raw = request.form.get("loan_id", "")
    if not loan_id_raw.isdigit():
        return redirect(url_for("student_loans"))
    loan_id = int(loan_id_raw)

    with _state_lock:
        tool = next(
            (t for t, items in state["rented"].items() for item in items
             if item.get("loan_id") == loan_id and item.get("uid") == uid),
            None,
        )
        if tool is not None:
            state["returns"][uid] = {
                "tool": tool, "loan_id": loan_id, "expires_at": time.time() + 60, "tagged": False,
            }
    return redirect(url_for("student_loans"))


@app.route("/me/return/cancel", methods=["POST"])
@student_login_required
def student_return_cancel():
    uid = session["student_uid"]
    with _state_lock:
        state["returns"].pop(uid, None)
    return redirect(url_for("student_loans"))


@app.route("/me/vapid")
@student_login_required
def student_vapid():
    return jsonify({"publicKey": CONFIG["vapid_public_key"]})


@app.route("/me/push", methods=["POST"])
@student_login_required
def student_push_subscribe():
    uid = session["student_uid"]
    sub = request.get_json(silent=True) or {}
    endpoint = sub.get("endpoint")
    keys = sub.get("keys")
    if not endpoint or not keys:
        return jsonify({"error": "invalid subscription"}), 400
    conn = db.get_conn(DB_PATH)
    try:
        db.add_subscription(conn, uid, endpoint, json.dumps(keys))
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/me/push/unsubscribe", methods=["POST"])
@student_login_required
def student_push_unsubscribe():
    endpoint = (request.get_json(silent=True) or {}).get("endpoint")
    if not endpoint:
        return jsonify({"error": "endpoint required"}), 400
    conn = db.get_conn(DB_PATH)
    try:
        db.remove_subscription(conn, endpoint)
    finally:
        conn.close()
    return jsonify({"ok": True})


def reminder_loop():
    """60초 주기로 미반납 loan을 훑어 아침/기한/연체 알림을 발송하는 데몬 스레드 (확장계획.md §E3)."""
    while True:
        time.sleep(60)
        try:
            pending = []  # (uid, message) — 발송은 락 해제 후
            with _state_lock:
                conn = db.get_conn(DB_PATH)
                try:
                    open_loans = [dict(row) for row in db.get_open_loans(conn)]
                    for loan in open_loans:
                        # DB에는 영문 클래스명이 저장됨 — 알림 문구는 즉시 알림과 동일하게 한국어 라벨 사용
                        loan["tool"] = CONFIG.get("tool_labels", {}).get(loan["tool"], loan["tool"])
                    sent = set()
                    for loan in open_loans:
                        for kind in ("morning", "due", "overdue"):
                            if db.has_notice(conn, loan["id"], kind):
                                sent.add((loan["id"], kind))
                    reminders = push.select_reminders(
                        open_loans, sent, datetime.now(), CONFIG.get("morning_notify_time", "09:00"),
                        CONFIG.get("overdue_grace_sec", 1800),
                    )
                    for loan_id, uid, kind, message in reminders:
                        if uid:
                            pending.append((uid, message))
                        db.mark_notice(conn, loan_id, kind)
                finally:
                    conn.close()
            flush_pushes(pending)
        except Exception as e:
            print(f"[push] 리마인더 루프 오류: {e}")


def _selfcheck():
    """판정 순수 함수 검증: 검출 개수 시퀀스 → 기대한 OUT/IN 이벤트 시퀀스."""
    registered = {"스패너": 1}
    debounce_frames = 3

    # 3프레임 연속 미검출 -> OUT 확정
    d_state = {}
    events = []
    for detected in [{"스패너": 1}, {"스패너": 1}, {"스패너": 0}, {"스패너": 0}, {"스패너": 0}]:
        d_state, events = judge_tools(detected, registered, debounce_frames, d_state)
    assert events == [{"tool": "스패너", "type": "OUT"}], events

    # 단발 흔들림(1프레임)은 3연속 조건을 못 채워 확정되지 않아야 함
    d_state = {}
    all_events = []
    for detected in [{"스패너": 1}, {"스패너": 1}, {"스패너": 0}, {"스패너": 1}, {"스패너": 1}, {"스패너": 1}]:
        d_state, events = judge_tools(detected, registered, debounce_frames, d_state)
        all_events.extend(events)
    assert all_events == [], all_events

    # OUT 확정 후 다시 3프레임 연속 검출 -> IN 확정
    d_state = {}
    events_seq = []
    for detected in [{"스패너": 0}] * 3 + [{"스패너": 1}] * 3:
        d_state, events = judge_tools(detected, registered, debounce_frames, d_state)
        events_seq.extend(events)
    assert events_seq == [{"tool": "스패너", "type": "OUT"}, {"tool": "스패너", "type": "IN"}], events_seq

    # light/buzzer: 대여 중이면 yellow, 전부 반납이면 green, buzzer는 S4 이전까지 항상 off
    assert decide_response({"스패너": {"registered": 1, "detected": 0, "rented": 1}}) == {
        "light": "yellow", "buzzer": "off",
    }
    assert decide_response({"스패너": {"registered": 1, "detected": 1, "rented": 0}}) == {
        "light": "green", "buzzer": "off",
    }

    # --- S4: RFID 세션 중 OUT -> 귀속 / 세션 만료 후 OUT -> 미확인 ---
    sess = resolve_session(None, "U1", now=0, session_sec=30)
    assert attribute_out(sess, {"U1": "홍길동"}) == ("U1", "홍길동", False)
    sess_still_valid = resolve_session(sess, "", now=10, session_sec=30)  # 태그 없이 시간만 흐름, 아직 유효
    assert attribute_out(sess_still_valid, {"U1": "홍길동"}) == ("U1", "홍길동", False)
    sess_expired = resolve_session(sess, "", now=31, session_sec=30)  # 30초 경과 -> 만료
    assert sess_expired is None
    assert attribute_out(sess_expired, {"U1": "홍길동"}) == (None, "", True)

    # --- F7: uid_names에 없는 UID로 태깅 -> 세션은 유효해도 미확인 반출 취급 ---
    sess_unregistered = resolve_session(None, "U9", now=0, session_sec=30)
    assert attribute_out(sess_unregistered, {"U1": "홍길동"}) == ("U9", "", True)

    # --- S4: 세션 중 다른 UID 태그 -> 마지막 태그 우선(기존 세션 즉시 종료) ---
    sess_switch = resolve_session(sess, "U2", now=5, session_sec=30)
    assert sess_switch == {"uid": "U2", "expires_at": 35}

    # --- S4: 미반납 임계 초과 -> OVERDUE 1회만 발생 (중복 기록 없음) ---
    rented = {"스패너": [{"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": False, "unauth": False}]}
    rented, overdue_events = check_overdue(rented, overdue_sec=7200, now=7300)
    assert overdue_events == [{"tool": "스패너", "uid": "U1", "name": "홍길동", "loan_id": None}]
    rented, overdue_events_again = check_overdue(rented, overdue_sec=7200, now=7400)  # 이미 기록됨 -> 재발생 금지
    assert overdue_events_again == []

    # --- E2: due_at 설정 loan은 due_at 기준, 미설정 loan은 overdue_sec 기준으로 판정 ---
    rented_due = {
        "니퍼": [
            # due_at=50 설정 -> now=60이면 초과(overdue_sec=7200 기준으로는 아직 미달)
            {"uid": "U1", "name": "홍길동", "out_time": 0, "due_at": 50, "cleared": False, "overdue_logged": False, "unauth": False},
            # due_at 미설정 -> overdue_sec=7200 기준 아직 미달(now=60)
            {"uid": "U2", "name": "김철수", "out_time": 0, "due_at": None, "cleared": False, "overdue_logged": False, "unauth": False},
        ]
    }
    _, due_events = check_overdue(rented_due, overdue_sec=7200, now=60)
    assert due_events == [{"tool": "니퍼", "uid": "U1", "name": "홍길동", "loan_id": None}], due_events

    # --- F9: 이미 빈 리스트인 공구는 new_rented에 잔류시키지 않아야 함 ---
    rented_to_empty = {"스패너": []}
    new_rented, _ = check_overdue(rented_to_empty, overdue_sec=7200, now=100)
    assert new_rented == {}, new_rented

    # --- S4: light/buzzer 우선순위 (미반납+정상대여 동시 -> red / 미확인+미반납 동시 -> buzzer=unauth) ---
    tool_status_mixed = {
        "스패너": {"registered": 1, "detected": 0, "rented": 1},    # 정상 대여 중
        "드라이버": {"registered": 1, "detected": 0, "rented": 1},  # 미반납 초과
    }
    overdue_only = [{"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": True, "unauth": False}]
    assert decide_response(tool_status_mixed, overdue_only) == {
        "light": "red", "buzzer": "overdue",
    }

    overdue_and_unauth = [
        {"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": True, "unauth": False},   # 미반납
        {"uid": "", "name": "", "out_time": 7290, "cleared": False, "overdue_logged": False, "unauth": True},        # 미확인
    ]
    assert decide_response(tool_status_mixed, overdue_and_unauth) == {
        "light": "red", "buzzer": "unauth",
    }

    # 수동 해제(cleared) 시 red/buzzer 해제되어야 함
    cleared = [{"uid": "", "name": "", "out_time": 0, "cleared": True, "overdue_logged": False, "unauth": True}]
    assert decide_response(tool_status_mixed, cleared) == {
        "light": "yellow", "buzzer": "off",
    }

    # --- F2: 재고 변경으로 confirmed=None(재기준선 대기) 상태 -> streak 도달 시 이벤트 없이 조용히 채택 ---
    d_state_reset = {"스패너": {"confirmed": None, "candidate": 0, "streak": 0}}
    reset_events = []
    for detected in [{"스패너": 2}] * 3:
        d_state_reset, ev = judge_tools(detected, {"스패너": 2}, debounce_frames, d_state_reset)
        reset_events.extend(ev)
    assert reset_events == [], reset_events
    assert d_state_reset["스패너"]["confirmed"] == 0, d_state_reset

    # --- E1: DB 왕복 — OUT->IN->OVERDUE 시퀀스가 loans/events에 기대한 행으로 남는지 + 재시작(재연결) 후 rented 복원 ---
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        db.init_db(tmp_path)
        conn = db.get_conn(tmp_path)
        db.add_user(conn, "U1", "홍길동")

        loan_id = db.insert_loan(conn, "nipper", "U1", "2026-01-01 09:00:00", unauth=False, snapshot_path="snap.jpg")
        db.add_event(conn, "OUT", "nipper", uid="U1", loan_id=loan_id, snapshot_path="snap.jpg")
        assert [row["id"] for row in db.get_open_loans(conn)] == [loan_id]

        db.close_loan(conn, loan_id, "2026-01-01 10:00:00")
        db.add_event(conn, "IN", "nipper", uid="U1", loan_id=loan_id)
        assert db.get_open_loans(conn) == []

        db.mark_overdue(conn, loan_id)
        db.add_event(conn, "OVERDUE", "nipper", uid="U1", loan_id=loan_id)

        recent = db.get_recent_events(conn, n=10)
        assert [e["이벤트"] for e in recent] == ["OVERDUE", "IN", "OUT"], recent
        assert recent[-1]["이름"] == "홍길동", recent
        conn.close()

        # 재시작 모사: 반납되지 않은 loan 하나를 추가로 남기고 새 커넥션으로 재연결해 rented 복원 확인
        conn2 = db.get_conn(tmp_path)
        loan_id2 = db.insert_loan(conn2, "driver", "", "2026-01-01 11:00:00", unauth=True, snapshot_path=None)
        conn2.close()

        conn3 = db.get_conn(tmp_path)
        restored = restore_rented_state(conn3)
        conn3.close()
        assert list(restored.keys()) == ["driver"], restored
        assert restored["driver"][0]["loan_id"] == loan_id2
        assert restored["driver"][0]["unauth"] is True

        # --- 자가 가입(pending) -> 관리자 카드 등록(assign_uid) ---
        conn4 = db.get_conn(tmp_path)
        pending_uid = db.create_pending_user(conn4, "99999", "김철수", "hash")
        assert pending_uid == "pending:99999"
        found = db.get_user_by_student(conn4, "99999")
        assert found["uid"] == pending_uid and found["password_hash"] == "hash", found
        db.assign_uid(conn4, pending_uid, "REALCARD1")
        assert db.get_user(conn4, "REALCARD1") is not None
        assert db.get_user(conn4, pending_uid) is None
        conn4.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    # --- E3: 아침/기한/연체 리마인더 선별 (각 1건) + 중복 방지 + due_at 없는 loan 제외 ---
    reminder_loans = [
        {"id": 10, "uid": "U1", "tool": "니퍼", "due_at": "2026-07-17 18:00:00"},
        {"id": 11, "uid": "U2", "tool": "펜치", "due_at": None},  # 기한 미설정 -> 대상 아님
    ]
    morning_reminders = push.select_reminders(
        reminder_loans, sent=set(), now=datetime(2026, 7, 17, 9, 30, 0), morning_time="09:00"
    )
    assert morning_reminders == [(10, "U1", "morning", "니퍼 오늘 반납 기한입니다")], morning_reminders

    due_reminders = push.select_reminders(
        reminder_loans, sent={(10, "morning")}, now=datetime(2026, 7, 17, 18, 0, 0), morning_time="09:00"
    )
    assert due_reminders == [(10, "U1", "due", "니퍼 반납 기한이 도래했습니다")], due_reminders

    overdue_reminders = push.select_reminders(
        reminder_loans, sent={(10, "morning"), (10, "due")},
        now=datetime(2026, 7, 17, 18, 31, 0), morning_time="09:00",
    )
    assert overdue_reminders == [(10, "U1", "overdue", "니퍼 반납이 지연되고 있습니다")], overdue_reminders

    # 이미 전부 발송됨 -> 재선별 안 됨
    no_reminders = push.select_reminders(
        reminder_loans, sent={(10, "morning"), (10, "due"), (10, "overdue")},
        now=datetime(2026, 7, 17, 18, 31, 0), morning_time="09:00",
    )
    assert no_reminders == [], no_reminders

    # --- /frame·로그인 라우트 통합 확인 (YOLO 추론 경로는 타지 않는 케이스만) ---
    client = app.test_client()
    if CONFIG.get("frame_token"):
        resp = client.post("/frame")
        assert resp.status_code == 401, resp.status_code
    resp = client.post("/frame", headers={"X-Frame-Token": CONFIG.get("frame_token", "")})
    assert resp.status_code == 400, resp.status_code
    assert client.get("/").status_code == 302
    assert client.get("/me/loans").status_code == 302
    assert client.post("/me/reserve").status_code == 302
    assert client.post("/me/return").status_code == 302

    # --- expire_returns 순수 함수: tagged 만료 -> 경보, untagged 만료 -> 무경보, 미만료 -> 유지 ---
    base_returns = {
        "U1": {"tool": "스패너", "loan_id": 1, "expires_at": 100, "tagged": True},
        "U2": {"tool": "니퍼", "loan_id": 2, "expires_at": 100, "tagged": False},
        "U3": {"tool": "드라이버", "loan_id": 3, "expires_at": 9999, "tagged": False},
    }
    remaining, alarm, failures = expire_returns(base_returns, now=200)
    assert alarm is True and failures == [{"uid": "U1", "tool": "스패너", "loan_id": 1}], (alarm, failures)
    assert remaining == {"U3": base_returns["U3"]}, remaining

    for _ in range(6):
        last_resp = client.post("/login", data={"password": "wrong"})
    assert "시도가 너무 많습니다" in last_resp.get_data(as_text=True)
    _login_fails.clear()

    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        db.init_db(DB_PATH)
        _conn = db.get_conn(DB_PATH)
        try:
            state["rented"] = restore_rented_state(_conn)  # 재시작 내성: 미반납 loan으로 큐 복원
        finally:
            _conn.close()
        threading.Thread(target=reminder_loop, daemon=True).start()
        from waitress import serve  # 개발 서버 대체 (TLS는 Funnel이 종단, 계획 외 옵션 튜닝 금지)
        serve(app, host=CONFIG.get("host", "0.0.0.0"), port=CONFIG.get("port", 5000), threads=8)
