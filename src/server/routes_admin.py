"""toolwatch 관리자 라우트 (S1~S5, E1). 대시보드 조회·로그인·제어·스냅샷 열람."""
import sqlite3
import time
from datetime import datetime
from functools import wraps

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for

import db
import push
from state import CONFIG, DB_PATH, ROOT_DIR, SNAPSHOT_DIR, VAPID_KEY_PATH, client_ip, debounce_state, login_blocked, record_login_result, save_config, state, state_lock

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
        # 프레임 자체는 /live.jpg로 따로 받는다 — 1초마다 페이지를 갱신하는데 여기에 base64로 실으면
        # 매 갱신마다 인코딩 비용과 수십~수백 KB 전송이 붙는다. 여기서는 표시 여부만 넘긴다.
        has_frame = bool(state["latest_frame_annot"] or state["latest_frame"])
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
            has_frame=has_frame,
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
                push.send_push(conn, uid, "toolwatch", "알림 테스트입니다", CONFIG, VAPID_KEY_PATH)
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


@bp.route("/live.jpg")
@login_required
def live_frame():
    """최신 프레임 1장을 JPEG 그대로 반환. 스냅샷 용도(디버깅·외부 도구)로 남겨둔다."""
    with state_lock:
        frame = state["latest_frame_annot"] or state["latest_frame"]
    if not frame:
        return "", 204
    # no-store가 없으면 브라우저가 캐시해 화면이 멈춘 것처럼 보인다
    return Response(frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@bp.route("/live.mjpg")
@login_required
def live_mjpeg():
    """MJPEG 스트림. <img>에 그대로 물리면 브라우저가 프레임을 이어서 그려 깜빡임이 없다.
    JS로 한 장씩 갈아끼우면 교체 순간마다 빈 칸이 보이는데, 그 문제가 원천적으로 사라진다.

    # ponytail: 열린 스트림 하나가 waitress 스레드 하나를 계속 점유한다. 관리자 대시보드라
    # 동시 접속이 많지 않아 server_threads 여유분으로 감당한다. 부족해지면 프레임 큐 + 단일
    # 브로드캐스트 스레드로 바꿔야 한다.
    """
    boundary = "toolwatchframe"
    crlf = chr(13) + chr(10)  # multipart 규격상 개행은 CRLF 고정

    def generate():
        last = None
        while True:
            with state_lock:
                frame = state["latest_frame_annot"] or state["latest_frame"]
            if frame is not None and frame is not last:
                last = frame
                part = ("--" + boundary + crlf
                        + "Content-Type: image/jpeg" + crlf
                        + "Content-Length: " + str(len(frame)) + crlf + crlf)
                yield part.encode() + frame + crlf.encode()
            else:
                time.sleep(0.02)  # 새 프레임이 없을 때만 쉰다 — 있으면 즉시 내보낸다

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=" + boundary,
        headers={"Cache-Control": "no-store"},
    )
