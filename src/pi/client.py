"""라즈베리파이 엣지 클라이언트 메인 루프.

설정된 주기(기본 3초)로 프레임을 캡처해 서버에 POST하고, 응답 JSON의 light/buzzer를
그대로 hw 모듈에 구동시킨다. 재고·경고 판정은 전부 서버가 수행한다 (계획서 3.2/3.6, 구현계획 §4).
RFID(RC522) 폴링은 별도 스레드 1개로 돌리며, 최근 태그 UID를 공유 변수에 기록한다.
"""
import io
import json
import threading
import time
from pathlib import Path

import requests
from mfrc522 import SimpleMFRC522
from picamera2 import Picamera2

import hw

CONFIG_PATH = Path(__file__).parent / "config.json"
REQUEST_TIMEOUT_SEC = 5  # 캡처 주기와 별개로 고정 — 계획에 없는 재시도/백오프 설계는 하지 않는다

_uid_lock = threading.Lock()
_latest_uid = ""


def _rfid_loop() -> None:
    """RC522 폴링 전용 스레드. 태그가 태깅될 때마다 최근 UID를 갱신한다.
    세션 유효기간(30초) 판단은 서버 책임이므로 여기서는 읽은 UID를 그대로 기록만 한다."""
    global _latest_uid
    reader = SimpleMFRC522()
    while True:
        try:
            uid, _ = reader.read()  # 태그가 태깅될 때까지 블로킹 — 전용 스레드라 메인 루프에 영향 없음
            with _uid_lock:
                _latest_uid = str(uid)
        except Exception:
            # SPI/배선 일시 오류로 스레드가 죽으면 이후 모든 반출이 미확인으로 오경고되므로 재시도
            time.sleep(1)


def _peek_uid() -> str:
    """대기 중인 UID를 비우지 않고 읽는다."""
    with _uid_lock:
        return _latest_uid


def _consume_uid(uid: str) -> None:
    """전송에 성공한 UID만 소비(clear)해 다음 주기에 재전송되지 않게 한다.
    전송 실패 시 비우면 사용자가 태그한 카드가 유실되어, 정상 대여가 '미확인 반출'로 오경고된다.
    요청 중에 새 카드가 태깅됐다면 UID가 달라지므로 그것은 지우지 않는다."""
    global _latest_uid
    if not uid:
        return
    with _uid_lock:
        if _latest_uid == uid:
            _latest_uid = ""


def _capture_jpeg(camera: Picamera2) -> bytes:
    # JPEG 품질은 capture_file 인자가 아니라 Picamera2.options로 지정한다 (run()에서 1회 설정).
    stream = io.BytesIO()
    camera.capture_file(stream, format="jpeg")
    return stream.getvalue()


def run() -> None:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    hw.init(config["relay_pins"])
    threading.Thread(target=_rfid_loop, daemon=True).start()

    camera = Picamera2()
    camera.configure(camera.create_still_configuration())
    camera.options["quality"] = config.get("jpeg_quality", 80)
    camera.start()

    url = config["server_url"]
    interval = config.get("capture_interval_sec", 3)
    session = requests.Session()
    if config.get("frame_token"):
        session.headers["X-Frame-Token"] = config["frame_token"]  # 서버 /frame 인증 토큰 (외부망 노출 대비)

    try:
        while True:
            start = time.monotonic()
            uid = _peek_uid()
            try:
                jpeg_bytes = _capture_jpeg(camera)
                resp = session.post(
                    url,
                    files={"image": ("frame.jpg", jpeg_bytes, "image/jpeg")},
                    data={"uid": uid},
                    timeout=REQUEST_TIMEOUT_SEC,
                )
                resp.raise_for_status()
                result = resp.json()
                _consume_uid(uid)
                hw.apply(result["light"], result["buzzer"])
                # 서버가 지시한 캡처 주기 반영(적응형 촬영) — 구서버 등 응답에 없으면 기존 주기 유지(하위 호환)
                interval = result.get("interval", interval)
            except Exception:
                # 카메라/GPIO 등 일시 오류 하나로 감시 루프 전체가 죽으면 안 됨(계획서 3.6).
                # 직전 경고 상태 유지, 다음 주기에 재시도. KeyboardInterrupt는 Exception이 아니므로 종료는 여전히 가능
                pass
            elapsed = time.monotonic() - start
            time.sleep(max(0.0, interval - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        camera.stop()
        hw.close()


if __name__ == "__main__":
    run()
