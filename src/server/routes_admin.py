"""toolwatch 관리자 라우트 (S1~S5, E1). 대시보드 조회·로그인·제어·스냅샷 열람."""
import base64
import sqlite3
import time
from datetime import datetime
from functools import wraps

from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for

import db
import push
from state import CONFIG, DB_PATH, ROOT_DIR, SNAPSHOT_DIR, client_ip, debounce_state, login_blocked, record_login_result, save_config, state, state_lock

bp = Blueprint("admin", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/")
@login_required
def dashboard():
    with state_lock:
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
            collecting=state["collecting"],
            collect_count=state["collect_count"],
        )


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = client_ip()
        if login_blocked(ip):
            error = "시도가 너무 많습니다. 잠시 후 다시 시도하세요"
        elif request.form.get("password") == CONFIG["hmi_password"]:
            record_login_result(ip, True)
            session["logged_in"] = True
            return redirect(url_for("admin.dashboard"))
        else:
            record_login_result(ip, False)
            error = "비밀번호가 틀렸습니다"
    return render_template("dashboard.html", logged_in=False, error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("admin.login"))


@bp.route("/control", methods=["POST"])
@login_required
def control():
    if request.form.get("action") == "push_test":
        # 상태를 건드리지 않고 네트워크 I/O만 하므로 state_lock 밖에서 처리 (푸시 지연이 /frame을 막지 않게)
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
        return redirect(url_for("admin.dashboard"))
    with state_lock:
        action = request.form.get("action")
        conn = db.get_conn(DB_PATH)
        try:
            if action == "stock":
                for tool in CONFIG["registered_stock"]:
                    value = request.form.get(f"stock_{tool}")
                    if value is not None and value.isdigit() and int(value) != CONFIG["registered_stock"][tool]:
                        CONFIG["registered_stock"][tool] = int(value)
                        # F2: 재고 수량이 실제로 바뀐 공구는 디바운스를 재기준선 대기 상태로 리셋 (유령 이벤트 방지)
                        debounce_state[tool] = {"confirmed": None, "candidate": 0, "streak": 0}
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
            elif action == "collect_start":
                session_dir = ROOT_DIR / "dataset" / "raw" / datetime.now().strftime("%Y%m%d_%H%M%S")
                session_dir.mkdir(parents=True, exist_ok=True)
                state["collecting"] = True
                state["collect_dir"] = session_dir
                state["collect_count"] = 0
                state["collect_last_size"] = 0
            elif action == "collect_stop":
                state["collecting"] = False
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
    return redirect(url_for("admin.dashboard"))


@bp.route("/snapshots/<path:filename>")
@login_required  # 반출 증거 사진이라 파일명이 추측 가능해도 로그인 없이는 열람 불가해야 한다
def snapshot_file(filename):
    return send_from_directory(SNAPSHOT_DIR, filename)
