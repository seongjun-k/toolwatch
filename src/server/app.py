"""toolwatch 서버 (S1~S5).

/frame 수신 → YOLO 추론(ROI 크롭) → 등록 재고 대비 검출 개수 대조 → 디바운스로
OUT/IN 확정 → RFID 세션 귀속 → 미반납/미확인 경고 → CSV 기록 + 반출 스냅샷 저장.
`/` 는 조회 겸 제어 대시보드(고정 비밀번호 로그인).
"""
import base64
import csv
import io
import json
import sys
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
CSV_PATH = ROOT_DIR / "logs" / "events.csv"
CSV_HEADER = ["시각", "이벤트", "공구", "UID", "이름", "스냅샷경로"]

with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)

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
}
_debounce_state = {}
_model = None  # ultralytics 모델 캐시, 최초 추론 시점까지 지연 로딩


def judge_tools(detected_counts, registered_stock, debounce_frames, debounce_state):
    """순수 함수: 검출 개수와 이전 디바운스 상태 → (새 디바운스 상태, 확정 이벤트 목록).

    공구별로 대여 중 개수를 `등록 재고 - 검출 개수`로 계산하고, 그 값이 N프레임
    연속 동일하게 관찰되어야만 직전 확정값과의 차이만큼 OUT/IN 이벤트를 낸다
    (계획서 §3.2). I/O 없음 — CSV 기록·스냅샷 저장은 호출부에서 처리한다.
    """
    new_state = {}
    events = []
    for tool, registered in registered_stock.items():
        detected = detected_counts.get(tool, 0)
        rented_now = max(0, registered - detected)  # 오검출로 등록 수보다 많이 잡혀도 0 아래로는 안 내려감

        prev = debounce_state.get(tool, {"confirmed": 0, "candidate": 0, "streak": 0})
        streak = prev["streak"] + 1 if rented_now == prev["candidate"] else 1
        confirmed = prev["confirmed"]

        if streak >= debounce_frames and rented_now != confirmed:
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
    """순수 함수: 유효한 세션이면 (uid, 이름), 없으면(=미확인 반출) (None, "")."""
    if session_ is None:
        return None, ""
    uid = session_["uid"]
    return uid, uid_names.get(uid, "")


def check_overdue(rented_by_tool, overdue_sec, now):
    """순수 함수: 대여 중 항목들에서 이번에 새로 미반납 초과로 전환된 것만 골라낸다.

    이미 기록한 항목은 overdue_logged로 표시해 다음 프레임에서 중복 기록하지 않는다
    (계획서 §3.2 미반납 경고, CSV는 초과 판정 순간 1회만).
    """
    new_rented = {}
    events = []
    for tool, items in rented_by_tool.items():
        new_items = []
        for item in items:
            item = dict(item)
            if not item["cleared"] and not item["overdue_logged"] and now - item["out_time"] > overdue_sec:
                events.append({"tool": tool, "uid": item["uid"], "name": item["name"]})
                item["overdue_logged"] = True
            new_items.append(item)
        new_rented[tool] = new_items
    return new_rented, events


def decide_response(tool_status, rented_items=(), now=0, overdue_sec=float("inf")):
    """light/buzzer 판정을 모아두는 단일 지점 (우선순위 적 > 황 > 녹, 계획서 §3.2).

    red: 해제되지 않은 미반납 초과 또는 미확인 반출이 하나라도 있을 때.
    buzzer: 미확인 반출이면 unauth, 아니면 미반납 초과면 overdue, 아니면 off (둘 다면 unauth 우선).
    """
    active = [r for r in rented_items if not r["cleared"]]
    has_unauth = any(r["uid"] == "" for r in active)
    has_overdue = any(now - r["out_time"] > overdue_sec for r in active)

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
    counts = {}
    for cls_id in r.boxes.cls.tolist():
        name = r.names[int(cls_id)]
        counts[name] = counts.get(name, 0) + 1
    return counts


def save_snapshot(tool, frame_bytes):
    """반출 확정 순간의 프레임(이미 받은 바이트)을 그대로 저장 — 재촬영·재추론 없음."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{tool}.jpg"
    (SNAPSHOT_DIR / filename).write_bytes(frame_bytes)
    return f"{SNAPSHOT_DIR.relative_to(ROOT_DIR).as_posix()}/{filename}"


def append_event(event_type, tool, uid="", name="", snapshot_path=""):
    is_new = not CSV_PATH.exists()
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    # BOM(utf-8-sig)은 파일을 새로 만들 때만. append 모드로 utf-8-sig를 쓰면 매 행 앞에 BOM이 다시 붙는다
    # (엑셀 한글 깨짐 방지용 BOM은 파일 선두에 한 번이면 충분)
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig" if is_new else "utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(CSV_HEADER)
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), event_type, tool, uid, name, snapshot_path])


def read_recent_events(n=20):
    """대시보드 이력 표시용 — CSV를 통째로 읽어 최근 n행만 최신순으로 반환 (4주 규모 로그라 tail 최적화 불필요)."""
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        path = row.get("스냅샷경로", "")
        row["snapshot_filename"] = Path(path).name if path else ""
    return list(reversed(rows[-n:]))


def save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/frame", methods=["POST"])
def receive_frame():
    image_file = request.files.get("image")
    if image_file is None:
        return jsonify({"error": "image required"}), 400
    frame_bytes = image_file.read()
    uid = request.form.get("uid", "")

    now = time.time()
    state["rfid_session"] = resolve_session(state["rfid_session"], uid, now, CONFIG["rfid_session_sec"])

    detected_counts = detect_tools(frame_bytes)

    global _debounce_state
    _debounce_state, events = judge_tools(
        detected_counts, CONFIG["registered_stock"], CONFIG["debounce_frames"], _debounce_state
    )

    for event in events:
        tool = event["tool"]
        if event["type"] == "OUT":
            snapshot_path = save_snapshot(tool, frame_bytes)
            attributed_uid, name = attribute_out(state["rfid_session"], CONFIG["uid_names"])
            append_event("OUT", tool, uid=attributed_uid or "", name=name, snapshot_path=snapshot_path)
            if attributed_uid is None:
                # OUT 행은 그대로 두고 미확인 반출만 별도 행으로 추가 기록 (대시보드 이력에서 구분용)
                append_event("UNAUTH", tool, snapshot_path=snapshot_path)
            state["rented"].setdefault(tool, []).append({
                "uid": attributed_uid or "", "name": name, "out_time": now,
                "cleared": False, "overdue_logged": False,
            })
        else:  # IN — 같은 공구 여러 개는 큐라 오래된 것부터 반납 처리
            queue = state["rented"].get(tool, [])
            item = queue.pop(0) if queue else {"uid": "", "name": ""}
            append_event("IN", tool, uid=item.get("uid", ""), name=item.get("name", ""))

    state["rented"], overdue_events = check_overdue(state["rented"], CONFIG["overdue_sec"], now)
    for ev in overdue_events:
        append_event("OVERDUE", ev["tool"], uid=ev["uid"], name=ev["name"])

    state["latest_frame"] = frame_bytes
    state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["tool_status"] = {
        tool: {
            "registered": registered,
            "detected": detected_counts.get(tool, 0),
            "rented": _debounce_state[tool]["confirmed"],
        }
        for tool, registered in CONFIG["registered_stock"].items()
    }

    rented_items = [item for items in state["rented"].values() for item in items]
    return jsonify(decide_response(state["tool_status"], rented_items, now, CONFIG["overdue_sec"]))


@app.route("/")
@login_required
def dashboard():
    now = time.time()
    rented_view = {
        tool: [
            {
                "uid": item["uid"] or "미확인",
                "name": item["name"] or "-",
                "elapsed_sec": int(now - item["out_time"]),
                "overdue": not item["cleared"] and now - item["out_time"] > CONFIG["overdue_sec"],
                "unauth": not item["cleared"] and item["uid"] == "",
            }
            for item in items
        ]
        for tool, items in state["rented"].items()
    }
    latest_frame_b64 = base64.b64encode(state["latest_frame"]).decode() if state["latest_frame"] else None
    return render_template(
        "dashboard.html",
        logged_in=True,
        tool_status=state["tool_status"],
        rented=rented_view,
        events=read_recent_events(),
        latest_frame_b64=latest_frame_b64,
        last_updated=state["last_updated"],
        config=CONFIG,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == CONFIG["hmi_password"]:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        error = "비밀번호가 틀렸습니다"
    return render_template("dashboard.html", logged_in=False, error=error)


@app.route("/control", methods=["POST"])
@login_required
def control():
    action = request.form.get("action")
    if action == "stock":
        for tool in CONFIG["registered_stock"]:
            value = request.form.get(f"stock_{tool}")
            if value is not None and value.isdigit():
                CONFIG["registered_stock"][tool] = int(value)
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
                item["cleared"] = True
    elif action == "uid_add":
        uid = request.form.get("uid", "").strip()
        name = request.form.get("name", "").strip()
        if uid and name:
            CONFIG["uid_names"][uid] = name
            save_config()
    elif action == "uid_delete":
        CONFIG["uid_names"].pop(request.form.get("uid", ""), None)
        save_config()
    return redirect(url_for("dashboard"))


@app.route("/snapshots/<path:filename>")
@login_required  # 반출 증거 사진이라 파일명이 추측 가능해도 로그인 없이는 열람 불가해야 한다
def snapshot_file(filename):
    return send_from_directory(SNAPSHOT_DIR, filename)


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
    assert attribute_out(sess, {"U1": "홍길동"}) == ("U1", "홍길동")
    sess_still_valid = resolve_session(sess, "", now=10, session_sec=30)  # 태그 없이 시간만 흐름, 아직 유효
    assert attribute_out(sess_still_valid, {"U1": "홍길동"}) == ("U1", "홍길동")
    sess_expired = resolve_session(sess, "", now=31, session_sec=30)  # 30초 경과 -> 만료
    assert sess_expired is None
    assert attribute_out(sess_expired, {"U1": "홍길동"}) == (None, "")

    # --- S4: 세션 중 다른 UID 태그 -> 마지막 태그 우선(기존 세션 즉시 종료) ---
    sess_switch = resolve_session(sess, "U2", now=5, session_sec=30)
    assert sess_switch == {"uid": "U2", "expires_at": 35}

    # --- S4: 미반납 임계 초과 -> OVERDUE 1회만 발생 (중복 기록 없음) ---
    rented = {"스패너": [{"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": False}]}
    rented, overdue_events = check_overdue(rented, overdue_sec=7200, now=7300)
    assert overdue_events == [{"tool": "스패너", "uid": "U1", "name": "홍길동"}]
    rented, overdue_events_again = check_overdue(rented, overdue_sec=7200, now=7400)  # 이미 기록됨 -> 재발생 금지
    assert overdue_events_again == []

    # --- S4: light/buzzer 우선순위 (미반납+정상대여 동시 -> red / 미확인+미반납 동시 -> buzzer=unauth) ---
    tool_status_mixed = {
        "스패너": {"registered": 1, "detected": 0, "rented": 1},    # 정상 대여 중
        "드라이버": {"registered": 1, "detected": 0, "rented": 1},  # 미반납 초과
    }
    overdue_only = [{"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": False}]
    assert decide_response(tool_status_mixed, overdue_only, now=7300, overdue_sec=7200) == {
        "light": "red", "buzzer": "overdue",
    }

    overdue_and_unauth = [
        {"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": False},   # 미반납
        {"uid": "", "name": "", "out_time": 7290, "cleared": False, "overdue_logged": False},         # 미확인
    ]
    assert decide_response(tool_status_mixed, overdue_and_unauth, now=7300, overdue_sec=7200) == {
        "light": "red", "buzzer": "unauth",
    }

    # 수동 해제(cleared) 시 red/buzzer 해제되어야 함
    cleared = [{"uid": "", "name": "", "out_time": 0, "cleared": True, "overdue_logged": False}]
    assert decide_response(tool_status_mixed, cleared, now=7300, overdue_sec=7200) == {
        "light": "yellow", "buzzer": "off",
    }

    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        app.run(host=CONFIG.get("host", "0.0.0.0"), port=CONFIG.get("port", 5000))
