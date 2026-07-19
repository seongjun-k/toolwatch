"""toolwatch 전역 상태·설정 (S1~S5, E1~E3).

전역 가변 상태(메모리 state/디바운스/락)와 config.json 로드 결과를 이 모듈 하나에만
둔다 — core.py/routes_*.py가 전부 이 모듈을 참조하고, 이 모듈은 그 반대로 core/routes를
참조하지 않아 순환 임포트가 생기지 않는다.
"""
import json
import threading
import time
from pathlib import Path

from flask import request

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
    "collecting": False,  # 데이터 수집 모드 on/off
    "collect_dir": None,  # Path, 수집 세션 폴더 (dataset/raw/YYYYMMDD_HHMMSS)
    "collect_count": 0,  # 현재 세션 저장 장수
    "collect_last_size": 0,  # 유사 프레임 솎기용 직전 저장 프레임 바이트 크기
}
debounce_state = {}
session_at_streak = {}  # 공구별 스트릭 시작 시점의 rfid_session 스냅샷 (F6: 확정 시점이 아닌 시작 시점 세션으로 귀속)
frame_at_streak = {}  # 공구별 스트릭 시작 시점의 frame_bytes 스냅샷 (반출 확정 시점엔 손이 이미 사라져 증거로 부적합)
prev_frame_at_streak = {}  # 공구별 스트릭 시작 직전 프레임 — 공구가 아직 있던(=집는 동작 중일 확률이 가장 높은) 장면
# ponytail: 전역 락 — 요청 빈도(3초 주기+대시보드)가 낮아 충분, 병목 시 세분화
state_lock = threading.Lock()


def save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


def flush_pushes(pending):
    """모아둔 [(uid, body)] 푸시를 발송한다 — send_push는 네트워크 I/O라 state_lock을 잡은 채
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


def client_ip():
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


def reset_login_fails():
    """selfcheck 전용: 테스트가 남긴 잠금 카운터를 정리."""
    _login_fails.clear()
