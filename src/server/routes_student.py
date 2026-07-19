"""toolwatch 학생용 라우트 (확장계획.md §E2/§E3). 로그인·계정생성·예약·반납·푸시 구독."""
import json
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db
from state import CONFIG, DB_PATH, client_ip, login_blocked, record_login_result, state, state_lock

bp = Blueprint("student", __name__, url_prefix="/me")


def student_login_required(view):
    """관리자 login_required와 별개 — session["student_uid"] 기준 (확장계획.md §E2)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("student_uid"):
            return redirect(url_for("student.student_login"))
        return view(*args, **kwargs)
    return wrapped


@bp.route("", methods=["GET", "POST"])
def student_login():
    error = None
    if request.method == "POST":
        if request.form.get("action") == "logout":
            session.pop("student_uid", None)
            return redirect(url_for("student.student_login"))
        ip = client_ip()
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
            return redirect(url_for("student.student_loans"))
        record_login_result(ip, False)
        if user and not user["password_hash"]:
            error = "계정이 없습니다. 먼저 계정 생성을 해주세요"
        else:
            error = "등록된 학번/비밀번호와 일치하지 않습니다"
    elif session.get("student_uid"):
        return redirect(url_for("student.student_loans"))
    return render_template("student.html", page="login", error=error)


@bp.route("/signup", methods=["POST"])
def student_signup():
    ip = client_ip()
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
                return redirect(url_for("student.student_loans"))
            elif user["name"] != name:
                error = "관리자에게 등록된 학번/이름이 아닙니다"
            elif user["password_hash"]:
                error = "이미 계정이 있습니다"
            else:
                db.set_user_password(conn, user["uid"], generate_password_hash(password))
                record_login_result(ip, True)
                session["student_uid"] = user["uid"]
                return redirect(url_for("student.student_loans"))
        finally:
            conn.close()
    record_login_result(ip, False)
    return render_template("student.html", page="login", error=error)


@bp.route("/loans")
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

    with state_lock:
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


@bp.route("/reserve", methods=["POST"])
@student_login_required
def student_reserve():
    uid = session["student_uid"]
    tool = request.form.get("tool", "")
    due_day_raw = request.form.get("due_day", "")  # 0=오늘 ~ 3=3일 후 (탭 화이트리스트)
    due_time_raw = request.form.get("due_time", "")  # HH:MM — 시각은 상세 지정 허용
    if tool not in CONFIG["registered_stock"] or due_day_raw not in ("0", "1", "2", "3"):
        return redirect(url_for("student.student_loans"))
    try:
        hh, mm = (int(x) for x in due_time_raw.split(":"))
        due_dt = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=int(due_day_raw))
    except ValueError:
        return redirect(url_for("student.student_loans"))
    due_epoch = due_dt.timestamp()
    if due_epoch <= time.time():  # 오늘 탭에 이미 지난 시각 조합 거부
        flash("과거로 시간여행을 하셨습니다. 반납 기한은 미래로 잡아주세요.")
        return redirect(url_for("student.student_loans"))

    with state_lock:
        state["reservations"][uid] = {
            "tool": tool,
            "due_epoch": due_epoch,
            "due_str": datetime.fromtimestamp(due_epoch).strftime("%Y-%m-%d %H:%M:%S"),
            "expires_at": time.time() + 60,
        }
    return redirect(url_for("student.student_loans"))


@bp.route("/reserve/cancel", methods=["POST"])
@student_login_required
def student_reserve_cancel():
    uid = session["student_uid"]
    with state_lock:
        state["reservations"].pop(uid, None)
    return redirect(url_for("student.student_loans"))


@bp.route("/return", methods=["POST"])
@student_login_required
def student_return():
    uid = session["student_uid"]
    loan_id_raw = request.form.get("loan_id", "")
    if not loan_id_raw.isdigit():
        return redirect(url_for("student.student_loans"))
    loan_id = int(loan_id_raw)

    with state_lock:
        tool = next(
            (t for t, items in state["rented"].items() for item in items
             if item.get("loan_id") == loan_id and item.get("uid") == uid),
            None,
        )
        if tool is not None:
            state["returns"][uid] = {
                "tool": tool, "loan_id": loan_id, "expires_at": time.time() + 60, "tagged": False,
            }
    return redirect(url_for("student.student_loans"))


@bp.route("/return/cancel", methods=["POST"])
@student_login_required
def student_return_cancel():
    uid = session["student_uid"]
    with state_lock:
        state["returns"].pop(uid, None)
    return redirect(url_for("student.student_loans"))


@bp.route("/vapid")
@student_login_required
def student_vapid():
    return jsonify({"publicKey": CONFIG["vapid_public_key"]})


@bp.route("/push", methods=["POST"])
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


@bp.route("/push/unsubscribe", methods=["POST"])
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
