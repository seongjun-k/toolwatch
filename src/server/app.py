"""toolwatch 서버 (S1~S5, E1).

/frame 수신 → YOLO 추론(ROI 크롭) → 등록 재고 대비 검출 개수 대조 → 디바운스로
OUT/IN 확정 → RFID 세션 귀속 → 미반납/미확인 경고 → DB(events/loans) 기록 + 반출 스냅샷 저장.
`/` 는 조회 겸 제어 대시보드(고정 비밀번호 로그인).

이 파일은 앱 생성·blueprint 등록·/frame 수신·reminder_loop 기동·_selfcheck 진입점만 담당한다.
판정 순수 함수·YOLO 추론은 core.py, 전역 상태·config는 state.py, 라우트는
routes_admin.py/routes_student.py에 있다.
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

import core
import db
import push
import state
from routes_admin import bp as admin_bp
from routes_student import bp as student_bp
from state import CONFIG, DB_PATH

app = Flask(__name__)
app.secret_key = CONFIG["secret_key"]
app.register_blueprint(admin_bp)
app.register_blueprint(student_bp)


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

    # YOLO 추론은 state를 읽지 않는 순수 계산 — 락 안에서 돌리면 추론 시간(CPU 수백 ms~초)만큼
    # 대시보드·학생 페이지가 전부 블로킹되므로 반드시 락 진입 전에 수행한다 (flush_pushes와 같은 원칙)
    detected_counts = core.detect_tools(frame_bytes)

    with state.state_lock:
        now = time.time()
        conn = db.get_conn(DB_PATH)
        try:
            state.state["rfid_session"] = core.resolve_session(state.state["rfid_session"], uid, now, CONFIG["rfid_session_sec"])

            # 반납 대기 중인 학생이 이번 프레임에 카드를 태그했으면 표시 (IN 미확정 시 경보 판정용)
            if uid and uid in state.state["returns"] and state.state["returns"][uid]["expires_at"] > now:
                state.state["returns"][uid]["tagged"] = True

            new_debounce_state, events = core.judge_tools(
                detected_counts, CONFIG["registered_stock"], CONFIG["debounce_frames"], state.debounce_state
            )
            # ponytail: 재바인딩 대신 제자리 갱신 — routes_admin.py가 같은 dict 객체를 import해
            # 참조하므로 여기서 새 dict로 바꿔치기하면 그쪽 참조가 낡은 객체를 가리키게 된다
            state.debounce_state.clear()
            state.debounce_state.update(new_debounce_state)

            # F6: 새로 스트릭이 시작된(=1) 공구는 이번 시점의 세션을 확정 귀속용으로 스냅샷
            # (디바운스 지연 약 9초 중 다른 사람이 태깅하면 확정 시점 세션으로는 오귀속되므로 시작 시점 세션 사용)
            for tool, d_state in state.debounce_state.items():
                if d_state["streak"] == 1:
                    state.session_at_streak[tool] = state.state["rfid_session"]
                    state.frame_at_streak[tool] = frame_bytes
                    state.prev_frame_at_streak[tool] = state.state["latest_frame"]  # 첫 프레임이면 None

            uid_names = db.list_users(conn)
            for event in events:
                tool = event["tool"]
                if event["type"] == "OUT":
                    # 확정 프레임 대신 감소가 처음 관측된(streak=1) 프레임을 증거로 저장 — 확정 시점엔 손이 이미 사라져 있음
                    snapshot_path = core.save_snapshot(tool, state.frame_at_streak.get(tool, frame_bytes))
                    # 직전 프레임(공구가 아직 있던 장면)도 함께 저장 — 평상 주기 3초 특성상 감소 관측
                    # 프레임엔 사람이 이미 화각 밖일 수 있어, 집는 동작이 담긴 직전 장면이 증거 가치가 더 높다
                    prev_bytes = state.prev_frame_at_streak.get(tool)
                    prev_path = core.save_snapshot(f"{tool}_prev", prev_bytes) if prev_bytes else snapshot_path
                    attributed_uid, name, unauth = core.attribute_out(state.session_at_streak.get(tool), uid_names)

                    # 예약 대여: 60초 내 본인 카드로 예약한 공구를 반출하면 반납 기한을 loan에 바로 반영
                    reservation = state.state["reservations"].get(attributed_uid) if attributed_uid else None
                    due_str = None
                    due_epoch = None
                    if reservation and reservation["expires_at"] > now and reservation["tool"] == tool:
                        due_str = reservation["due_str"]
                        due_epoch = reservation["due_epoch"]
                        state.state["reservations"].pop(attributed_uid, None)

                    loan_id = db.insert_loan(conn, tool, attributed_uid or "", core.now_str(), unauth, snapshot_path, due_at=due_str)
                    db.add_event(conn, "OUT", tool, uid=attributed_uid or "", loan_id=loan_id, snapshot_path=snapshot_path)
                    if unauth:
                        # OUT 행은 그대로 두고 미확인 반출만 별도 행으로 추가 기록 (대시보드 이력에서 구분용)
                        # UNAUTH 행에는 직전 프레임을 연결 — OUT 행(감소 관측 장면)과 함께 이력에서 두 장 모두 열람 가능
                        db.add_event(conn, "UNAUTH", tool, uid=attributed_uid or "", loan_id=loan_id, snapshot_path=prev_path)
                    if attributed_uid:
                        tool_label = CONFIG.get("tool_labels", {}).get(tool, tool)
                        pending_pushes.append((attributed_uid, f"{tool_label} 대여 처리됨"))
                    state.state["rented"].setdefault(tool, []).append({
                        "uid": attributed_uid or "", "name": name, "out_time": now, "due_at": due_epoch,
                        "cleared": False, "overdue_logged": False, "unauth": unauth, "loan_id": loan_id,
                    })
                else:  # IN — 같은 공구 여러 개는 큐라 오래된 것부터 반납 처리
                    queue = state.state["rented"].get(tool, [])
                    item = queue.pop(0) if queue else {"uid": "", "name": "", "loan_id": None}
                    if not queue:
                        state.state["rented"].pop(tool, None)  # F9: 비면 잔류시키지 않음
                    if item.get("loan_id") is not None:
                        db.close_loan(conn, item["loan_id"], core.now_str())
                    db.add_event(conn, "IN", tool, uid=item.get("uid", ""), loan_id=item.get("loan_id"))
                    if item.get("uid"):
                        tool_label = CONFIG.get("tool_labels", {}).get(tool, tool)
                        pending_pushes.append((item["uid"], f"{tool_label} 반납 완료"))
                    # 정상 반납: 이 loan_id(또는 tool 일치)로 대기 중이던 반납 예약을 지운다
                    return_uid = next(
                        (
                            u for u, r in state.state["returns"].items()
                            if r["loan_id"] == item.get("loan_id") or r["tool"] == tool
                        ),
                        None,
                    )
                    if return_uid is not None:
                        state.state["returns"].pop(return_uid, None)

            state.state["rented"], overdue_events = core.check_overdue(state.state["rented"], CONFIG["overdue_sec"], now)
            for ev in overdue_events:
                if ev["loan_id"] is not None:
                    db.mark_overdue(conn, ev["loan_id"])
                db.add_event(conn, "OVERDUE", ev["tool"], uid=ev["uid"], loan_id=ev["loan_id"])

            # 반납 대기 만료 스캔: 태그했는데 IN 미확정이면 30초 경보 + RETURN_FAIL 1회 기록
            state.state["returns"], alarm, return_failures = core.expire_returns(state.state["returns"], now)
            if alarm:
                state.state["return_alarm_until"] = now + 30
            for fail in return_failures:
                db.add_event(conn, "RETURN_FAIL", fail["tool"], uid=fail["uid"], loan_id=fail["loan_id"])
        finally:
            conn.close()

        state.state["latest_frame"] = frame_bytes
        state.state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.state["tool_status"] = {
            tool: {
                "registered": registered,
                "detected": detected_counts.get(tool, 0),
                # 재기준선 대기 중(confirmed=None)엔 0 대신 실제 rented 큐 길이로 표시 — 재고 변경 직후
                # 수 초간 경광등이 실제로는 대여 중인데 정상(녹색)으로 잘못 보이는 것을 막는다
                "rented": (
                    state.debounce_state[tool]["confirmed"]
                    if state.debounce_state[tool]["confirmed"] is not None
                    else len(state.state["rented"].get(tool, []))
                ),
            }
            for tool, registered in CONFIG["registered_stock"].items()
        }

        rented_items = [item for items in state.state["rented"].values() for item in items]
        response = core.decide_response(state.state["tool_status"], rented_items)
        # 반납 실패 경보는 rented 플래그가 아닌 시한부 상태(return_alarm_until)라 decide_response의
        # 단일 판정 지점 밖에서 호출부가 덮어쓴다 (함수 시그니처는 그대로 유지)
        if now < state.state["return_alarm_until"]:
            response["light"] = "red"
            response["buzzer"] = "unauth"
        # 디바운스 진행 중(변화 관측 중)인 공구가 있으면 다음 캡처를 빠르게, 없으면 기본 주기 유지 지시
        in_progress = any(
            d["candidate"] != d["confirmed"] for d in state.debounce_state.values()
        )
        response["interval"] = CONFIG.get("fast_interval_sec", 0.5) if in_progress else CONFIG.get("capture_interval_sec", 3)

    state.flush_pushes(pending_pushes)
    return jsonify(response)


def reminder_loop():
    """60초 주기로 미반납 loan을 훑어 아침/기한/연체 알림을 발송하는 데몬 스레드 (확장계획.md §E3)."""
    while True:
        time.sleep(60)
        try:
            pending = []  # (uid, message) — 발송은 락 해제 후
            with state.state_lock:
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
            state.flush_pushes(pending)
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
        d_state, events = core.judge_tools(detected, registered, debounce_frames, d_state)
    assert events == [{"tool": "스패너", "type": "OUT"}], events

    # 단발 흔들림(1프레임)은 3연속 조건을 못 채워 확정되지 않아야 함
    d_state = {}
    all_events = []
    for detected in [{"스패너": 1}, {"스패너": 1}, {"스패너": 0}, {"스패너": 1}, {"스패너": 1}, {"스패너": 1}]:
        d_state, events = core.judge_tools(detected, registered, debounce_frames, d_state)
        all_events.extend(events)
    assert all_events == [], all_events

    # OUT 확정 후 다시 3프레임 연속 검출 -> IN 확정
    d_state = {}
    events_seq = []
    for detected in [{"스패너": 0}] * 3 + [{"스패너": 1}] * 3:
        d_state, events = core.judge_tools(detected, registered, debounce_frames, d_state)
        events_seq.extend(events)
    assert events_seq == [{"tool": "스패너", "type": "OUT"}, {"tool": "스패너", "type": "IN"}], events_seq

    # light/buzzer: 대여 중이면 yellow, 전부 반납이면 green, buzzer는 S4 이전까지 항상 off
    assert core.decide_response({"스패너": {"registered": 1, "detected": 0, "rented": 1}}) == {
        "light": "yellow", "buzzer": "off",
    }
    assert core.decide_response({"스패너": {"registered": 1, "detected": 1, "rented": 0}}) == {
        "light": "green", "buzzer": "off",
    }

    # --- S4: RFID 세션 중 OUT -> 귀속 / 세션 만료 후 OUT -> 미확인 ---
    sess = core.resolve_session(None, "U1", now=0, session_sec=30)
    assert core.attribute_out(sess, {"U1": "홍길동"}) == ("U1", "홍길동", False)
    sess_still_valid = core.resolve_session(sess, "", now=10, session_sec=30)  # 태그 없이 시간만 흐름, 아직 유효
    assert core.attribute_out(sess_still_valid, {"U1": "홍길동"}) == ("U1", "홍길동", False)
    sess_expired = core.resolve_session(sess, "", now=31, session_sec=30)  # 30초 경과 -> 만료
    assert sess_expired is None
    assert core.attribute_out(sess_expired, {"U1": "홍길동"}) == (None, "", True)

    # --- F7: uid_names에 없는 UID로 태깅 -> 세션은 유효해도 미확인 반출 취급 ---
    sess_unregistered = core.resolve_session(None, "U9", now=0, session_sec=30)
    assert core.attribute_out(sess_unregistered, {"U1": "홍길동"}) == ("U9", "", True)

    # --- S4: 세션 중 다른 UID 태그 -> 마지막 태그 우선(기존 세션 즉시 종료) ---
    sess_switch = core.resolve_session(sess, "U2", now=5, session_sec=30)
    assert sess_switch == {"uid": "U2", "expires_at": 35}

    # --- S4: 미반납 임계 초과 -> OVERDUE 1회만 발생 (중복 기록 없음) ---
    rented = {"스패너": [{"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": False, "unauth": False}]}
    rented, overdue_events = core.check_overdue(rented, overdue_sec=7200, now=7300)
    assert overdue_events == [{"tool": "스패너", "uid": "U1", "name": "홍길동", "loan_id": None}]
    rented, overdue_events_again = core.check_overdue(rented, overdue_sec=7200, now=7400)  # 이미 기록됨 -> 재발생 금지
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
    _, due_events = core.check_overdue(rented_due, overdue_sec=7200, now=60)
    assert due_events == [{"tool": "니퍼", "uid": "U1", "name": "홍길동", "loan_id": None}], due_events

    # --- F9: 이미 빈 리스트인 공구는 new_rented에 잔류시키지 않아야 함 ---
    rented_to_empty = {"스패너": []}
    new_rented, _ = core.check_overdue(rented_to_empty, overdue_sec=7200, now=100)
    assert new_rented == {}, new_rented

    # --- S4: light/buzzer 우선순위 (미반납+정상대여 동시 -> red / 미확인+미반납 동시 -> buzzer=unauth) ---
    tool_status_mixed = {
        "스패너": {"registered": 1, "detected": 0, "rented": 1},    # 정상 대여 중
        "드라이버": {"registered": 1, "detected": 0, "rented": 1},  # 미반납 초과
    }
    overdue_only = [{"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": True, "unauth": False}]
    assert core.decide_response(tool_status_mixed, overdue_only) == {
        "light": "red", "buzzer": "overdue",
    }

    overdue_and_unauth = [
        {"uid": "U1", "name": "홍길동", "out_time": 0, "cleared": False, "overdue_logged": True, "unauth": False},   # 미반납
        {"uid": "", "name": "", "out_time": 7290, "cleared": False, "overdue_logged": False, "unauth": True},        # 미확인
    ]
    assert core.decide_response(tool_status_mixed, overdue_and_unauth) == {
        "light": "red", "buzzer": "unauth",
    }

    # 수동 해제(cleared) 시 red/buzzer 해제되어야 함
    cleared = [{"uid": "", "name": "", "out_time": 0, "cleared": True, "overdue_logged": False, "unauth": True}]
    assert core.decide_response(tool_status_mixed, cleared) == {
        "light": "yellow", "buzzer": "off",
    }

    # --- F2: 재고 변경으로 confirmed=None(재기준선 대기) 상태 -> streak 도달 시 이벤트 없이 조용히 채택 ---
    d_state_reset = {"스패너": {"confirmed": None, "candidate": 0, "streak": 0}}
    reset_events = []
    for detected in [{"스패너": 2}] * 3:
        d_state_reset, ev = core.judge_tools(detected, {"스패너": 2}, debounce_frames, d_state_reset)
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
        restored = core.restore_rented_state(conn3)
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
    remaining, alarm, failures = core.expire_returns(base_returns, now=200)
    assert alarm is True and failures == [{"uid": "U1", "tool": "스패너", "loan_id": 1}], (alarm, failures)
    assert remaining == {"U3": base_returns["U3"]}, remaining

    for _ in range(6):
        last_resp = client.post("/login", data={"password": "wrong"})
    assert "시도가 너무 많습니다" in last_resp.get_data(as_text=True)
    state.reset_login_fails()

    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        db.init_db(DB_PATH)
        _conn = db.get_conn(DB_PATH)
        try:
            state.state["rented"] = core.restore_rented_state(_conn)  # 재시작 내성: 미반납 loan으로 큐 복원
        finally:
            _conn.close()
        # 복원된 대여 수로 디바운스 기준선을 시드 — 비워 두면 confirmed=0에서 출발해
        # 이미 반출된 공구에 대해 유령 OUT이 재확정되어 loan이 중복 생성된다
        state.debounce_state.update({
            tool: {"confirmed": len(items), "candidate": 0, "streak": 0}
            for tool, items in state.state["rented"].items()
        })
        threading.Thread(target=reminder_loop, daemon=True).start()
        from waitress import serve  # 개발 서버 대체 (TLS는 Funnel이 종단, 계획 외 옵션 튜닝 금지)
        serve(app, host=CONFIG.get("host", "0.0.0.0"), port=CONFIG.get("port", 5000), threads=8)
