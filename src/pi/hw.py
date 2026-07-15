"""릴레이 모듈 4채널(적/황/녹/부저) 제어 래퍼 (gpiozero OutputDevice).

3색 경광등은 12V 공통음극 소자라 GPIO에 직결할 수 없어 릴레이 모듈을 경유한다 (계획서 2.2).
판정 로직은 전부 서버가 수행하고, 여기서는 서버가 내려준 light/buzzer 값을 그대로 구동만 한다.
"""
from gpiozero import OutputDevice

# 릴레이 모듈이 액티브 로우(신호 LOW일 때 통전)인지 액티브 하이인지는 모듈 수령 후 실측으로 확정.
# 여기 하나만 뒤집으면 아래 on()/off() 호출부는 극성을 신경 쓸 필요가 없다.
RELAY_ACTIVE_LOW = True

_devices = {}
_last_state = (None, None)  # (light, buzzer) - 동일 상태 재적용 시 릴레이 클릭/블링크 재시작 방지


def init(pins: dict) -> None:
    """pins: {"red": int, "yellow": int, "green": int, "buzzer": int}"""
    global _devices, _last_state
    active_high = not RELAY_ACTIVE_LOW
    _devices = {
        name: OutputDevice(pin, active_high=active_high, initial_value=False)
        for name, pin in pins.items()
    }
    _last_state = (None, None)


def _set_light(color: str) -> None:
    """적/황/녹 중 하나만 ON (배타)."""
    for name in ("red", "yellow", "green"):
        (_devices[name].on if name == color else _devices[name].off)()


def _set_buzzer(pattern: str) -> None:
    """buzzer: off|overdue|unauth.
    overdue=연속음(on), unauth=단속음(blink). blink(background=True)는 별도 스레드로 비블로킹
    동작하므로 메인 루프의 3초 주기 캡처/전송을 막지 않는다."""
    buzzer = _devices["buzzer"]
    if pattern == "overdue":
        buzzer.on()
    elif pattern == "unauth":
        buzzer.blink(on_time=0.3, off_time=0.3, background=True)
    else:
        buzzer.off()


def apply(light: str, buzzer: str) -> None:
    """서버 응답의 light/buzzer를 그대로 구동한다. 직전과 동일한 상태면 아무 것도 하지 않는다
    (동일 상태를 매 주기 재적용하면 릴레이가 계속 클릭하거나 blink가 매번 재시작된다)."""
    global _last_state
    state = (light, buzzer)
    if state == _last_state:
        return
    _set_light(light)
    _set_buzzer(buzzer)
    _last_state = state


def close() -> None:
    """전 채널 OFF 후 GPIO 리소스 반환 (Ctrl+C 종료 시 호출).
    close()만 하면 핀이 input으로 되돌아가 릴레이 극성에 따라 경광등이 켜진 채 남을 수 있으므로
    반드시 off()를 먼저 호출한다."""
    for dev in _devices.values():
        dev.off()
        dev.close()
