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
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from PIL import Image

import db

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
DB_PATH = BASE_DIR / "toolwatch.db"  # selfcheck에서는 임시 파일 경로로 교체(구현계획.md §8)

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
_session_at_streak = {}  # 공구별 스트릭 시작 시점의 rfid_session 스냅샷 (F6: 확정 시점이 아닌 시작 시점 세션으로 귀속)
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

    with _state_lock:
        now = time.time()
        conn = db.get_conn(DB_PATH)
        try:
            state["rfid_session"] = resolve_session(state["rfid_session"], uid, now, CONFIG["rfid_session_sec"])

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

            uid_names = db.list_users(conn)
            for event in events:
                tool = event["tool"]
                if event["type"] == "OUT":
                    snapshot_path = save_snapshot(tool, frame_bytes)
                    attributed_uid, name, unauth = attribute_out(_session_at_streak.get(tool), uid_names)
                    loan_id = db.insert_loan(conn, tool, attributed_uid or "", now_str(), unauth, snapshot_path)
                    db.add_event(conn, "OUT", tool, uid=attributed_uid or "", loan_id=loan_id, snapshot_path=snapshot_path)
                    if unauth:
                        # OUT 행은 그대로 두고 미확인 반출만 별도 행으로 추가 기록 (대시보드 이력에서 구분용)
                        db.add_event(conn, "UNAUTH", tool, uid=attributed_uid or "", loan_id=loan_id, snapshot_path=snapshot_path)
                    state["rented"].setdefault(tool, []).append({
                        "uid": attributed_uid or "", "name": name, "out_time": now, "due_at": None,
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

            state["rented"], overdue_events = check_overdue(state["rented"], CONFIG["overdue_sec"], now)
            for ev in overdue_events:
                if ev["loan_id"] is not None:
                    db.mark_overdue(conn, ev["loan_id"])
                db.add_event(conn, "OVERDUE", ev["tool"], uid=ev["uid"], loan_id=ev["loan_id"])
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
        return jsonify(decide_response(state["tool_status"], rented_items))


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
        if request.form.get("password") == CONFIG["hmi_password"]:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
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
        student_id = request.form.get("student_id", "").strip()
        name = request.form.get("name", "").strip()
        conn = db.get_conn(DB_PATH)
        try:
            uid = db.find_uid_by_student(conn, student_id, name)
        finally:
            conn.close()
        if uid:
            session["student_uid"] = uid
            return redirect(url_for("student_loans"))
        error = "등록된 학번/이름과 일치하지 않습니다"
    elif session.get("student_uid"):
        return redirect(url_for("student_loans"))
    return render_template("student.html", page="login", error=error)


@app.route("/me/loans")
@student_login_required
def student_loans():
    uid = session["student_uid"]
    conn = db.get_conn(DB_PATH)
    try:
        uid_names = db.list_users(conn)
        rows = db.get_loans_by_uid(conn, uid)
    finally:
        conn.close()

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
                # datetime-local input value 형식(YYYY-MM-DDTHH:MM)으로 미리 채워줌
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
        open_loans=open_loans, returned_loans=returned_loans[:5],
        tool_labels=CONFIG.get("tool_labels", {}),
    )


@app.route("/me/due", methods=["POST"])
@student_login_required
def student_set_due():
    uid = session["student_uid"]
    loan_id_raw = request.form.get("loan_id", "")
    due_raw = request.form.get("due_at", "")  # <input type="datetime-local"> 값: YYYY-MM-DDTHH:MM
    if not loan_id_raw.isdigit() or not due_raw:
        return redirect(url_for("student_loans"))
    try:
        due_dt = datetime.strptime(due_raw, "%Y-%m-%dT%H:%M")
    except ValueError:
        return redirect(url_for("student_loans"))
    if due_dt.timestamp() <= time.time():  # 과거 시각 거부
        return redirect(url_for("student_loans"))
    loan_id = int(loan_id_raw)
    due_str = due_dt.strftime("%Y-%m-%d %H:%M:%S")

    with _state_lock:
        conn = db.get_conn(DB_PATH)
        try:
            # uid까지 WHERE에 넣어 DB 레벨에서도 본인 소유 확인 (신뢰 경계, 생략 불가)
            updated = db.set_loan_due(conn, loan_id, uid, due_str)
        finally:
            conn.close()
        if updated:
            for items in state["rented"].values():
                for item in items:
                    if item.get("loan_id") == loan_id:
                        item["due_at"] = due_dt.timestamp()
    return redirect(url_for("student_loans"))


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
    finally:
        tmp_path.unlink(missing_ok=True)

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
        app.run(host=CONFIG.get("host", "0.0.0.0"), port=CONFIG.get("port", 5000))
