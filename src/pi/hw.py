"""릴레이 모듈 4채널(적/황/녹/부저) 제어 래퍼 (gpiozero DigitalOutputDevice).

3색 경광등은 12V 공통음극 소자라 GPIO에 직결할 수 없어 릴레이 모듈을 경유한다 (계획서 2.2).
판정 로직은 전부 서버가 수행하고, 여기서는 서버가 내려준 light/buzzer 값을 그대로 구동만 한다.

채널을 끄는 방식: OFF를 핀 HIGH(3.3V)로 구동하지 않고 핀을 해제(cleanup, 입력 상태)한다.
3.3V HIGH로는 모듈 입력측 포토커플러가 약하게 켜진 채 남아 채널이 겹쳐 동작하는 것이 실측됐고,
해제하면 모듈 내부 풀업으로 완전히 꺼진다. 따라서 켜는 채널만 핀을 잡고(LOW=ON), 나머지는 전부 해제 상태다.
"""
import threading
import time

from gpiozero import DigitalOutputDevice

# 릴레이 모듈은 액티브 로우(신호 LOW일 때 통전, 점퍼 L)로 실측 확정. 해제(입력)=OFF 방식이 이 극성을 전제한다.
RELAY_ACTIVE_LOW = True

BLINK_SEC = 1  # 적색 깜빡임 주기(켜짐=꺼짐). 경광등 내부 캐패시터 때문에 꺼져도 바로 안 꺼져서 0.3초로는 깜빡임이 안 보인다
BUZZ_SEC = 3  # 경보 시작 시 부저 울림 시간(초)
ALL_OFF_SEC = 0.2  # 전 채널 해제 후 다음 채널을 켜기 전 대기 — 릴레이가 확실히 떨어질 시간

_pins = {}  # name -> BCM 번호
_devices = {}  # name -> 켜져 있는 채널의 DigitalOutputDevice (없으면 해제 상태=OFF)
_last_state = (None, None)  # (light, buzzer) - 동일 상태 재적용 시 릴레이 클릭/블링크 재시작 방지
_gen = 0  # apply() 호출 세대 — 새 호출이 오면 진행 중이던 이전 시퀀스가 남은 단계를 버린다
_lock = threading.Lock()


def init(pins: dict) -> None:
    """pins: {"red": int, "yellow": int, "green": int, "buzzer": int}. 핀은 아무것도 잡지 않은 채 시작한다."""
    global _pins, _devices, _last_state
    _pins = dict(pins)
    _devices = {}
    _last_state = (None, None)


def _on(name: str) -> None:
    """채널을 켠다: 핀을 잡고 곧바로 ON 레벨로 구동한다 (HIGH를 거치지 않는다)."""
    _devices[name] = DigitalOutputDevice(_pins[name], active_high=not RELAY_ACTIVE_LOW, initial_value=True)


def _release_all() -> None:
    """전 채널 cleanup: 핀을 해제한다."""
    for name in list(_devices):
        _devices.pop(name).close()


def _sequence(gen: int, light: str, buzzer: str) -> None:
    """한 번에 한 가지만 켠다: 전부 해제 -> (경보면 부저 BUZZ_SEC초 -> 전부 해제) -> 색.
    공통음극 경광등에서 부저·색이 겹쳐 켜지는 현상을 피하려고 시간을 나눈다. 그래서 경보 시작 후 처음
    BUZZ_SEC초는 부저만 울리고 색은 그 뒤에 켜진다. 적색은 서버가 red를 내려주는 동안 계속 깜빡인다.
    예외: 무단 반출(unauth)은 처음 BUZZ_SEC초 동안 부저와 적색을 깜빡임 없이 함께 켠 뒤 적색 깜빡임으로 넘어간다.
    buzzer: off|overdue|unauth. 부저 소자에 단속 회로가 내장돼 있어 켜기만 하면 단속음이 나므로
    패턴 구분 없이 on/off만 한다(두 경보는 소리가 아니라 색으로만 구분된다)."""
    def wait(sec: float) -> bool:
        end = time.monotonic() + sec
        while time.monotonic() < end:
            if gen != _gen:
                return False
            time.sleep(0.05)
        return gen == _gen

    if not wait(ALL_OFF_SEC):
        return
    if buzzer != "off":
        with _lock:
            if gen != _gen:
                return
            _on("buzzer")
            if buzzer == "unauth":
                _on("red")
        wait(BUZZ_SEC)
        with _lock:
            if gen != _gen:
                return  # 새 apply()가 이미 전 채널을 해제했다
            _release_all()
        if not wait(ALL_OFF_SEC):
            return
    if light != "red":
        with _lock:
            if gen == _gen:
                _on(light)
        return
    # 적색 깜빡임: 꺼짐 구간도 gpiozero blink처럼 핀을 HIGH로 구동하지 않고 cleanup(해제)으로 끊는다.
    # 새 apply()/close()가 오면 그쪽에서 이미 전 채널을 해제했으므로 wait()이 False가 되는 즉시 빠져나가면 된다
    while True:
        with _lock:
            if gen != _gen:
                return
            _on("red")
        if not wait(BLINK_SEC):
            return
        with _lock:
            if gen != _gen:
                return
            _release_all()
        if not wait(BLINK_SEC):
            return


def apply(light: str, buzzer: str) -> None:
    """서버 응답의 light/buzzer를 그대로 구동한다. 직전과 동일한 상태면 아무 것도 하지 않는다
    (동일 상태를 매 주기 재적용하면 릴레이가 계속 클릭하거나 blink가 매번 재시작된다).
    상태가 바뀌면 전 채널을 먼저 cleanup하고, 이후 단계는 별도 스레드에서 진행해 메인 루프를 막지 않는다."""
    global _last_state, _gen
    state = (light, buzzer)
    if state == _last_state:
        return
    _last_state = state
    with _lock:
        _gen += 1
        gen = _gen
        _release_all()
    threading.Thread(target=_sequence, args=(gen, light, buzzer), daemon=True).start()


def close() -> None:
    """전 채널 cleanup (종료 시 호출). 핀을 해제하면 입력 상태로 돌아가 릴레이가 전부 떨어진다.
    세대를 올려 진행 중인 시퀀스가 해제된 핀을 다시 잡지 않게 한다."""
    global _gen
    with _lock:
        _gen += 1
        _release_all()
