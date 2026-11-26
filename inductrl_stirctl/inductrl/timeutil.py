"""시각 도우미. 경과시간 계산은 monotonic 만 쓰고, UTC 는 기록용입니다."""
import time
from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_to_utc(text: str) -> str:
    """ISO 8601 시각을 UTC ISO 문자열로 바꿉니다.

    시간대가 없으면 MIC 시스템의 로컬 시간대로 해석합니다.
    """
    s = text.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("ISO 8601 형식이 아닙니다: {!r} (예: 2026-12-01T14:03:05+09:00)".format(text))
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 로컬 시간대 부여
    return dt.astimezone(timezone.utc).isoformat()


def utc_seconds_between(start_iso: str, end_iso: str) -> float:
    a = datetime.fromisoformat(start_iso)
    b = datetime.fromisoformat(end_iso)
    return (b - a).total_seconds()


class SimClock:
    """mock --fast 및 테스트용 가상 시계. sleep 하면 시간이 즉시 흐릅니다."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += seconds

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


def real_clock() -> float:
    return time.monotonic()


def real_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


class ScaledClock:
    """실시간 시뮬레이터용 배율 시계.

    1초의 설비 시간을 1/speed_factor 초의 실제 시간으로 압축합니다.
    기록되는 경과시간(elapsed_s)은 설비 시간 그대로라서, 10x 로 봐도 120초 배치로 남습니다.
    """

    def __init__(self, speed_factor: float, clock: Callable[[], float] = real_clock,
                 sleep: Callable[[float], None] = real_sleep) -> None:
        if speed_factor <= 0:
            raise ValueError("speed_factor 는 0 보다 커야 합니다")
        self.speed = float(speed_factor)
        self._clock = clock
        self._sleep = sleep
        self._inner_origin = clock()
        self._origin = 1000.0

    def now(self) -> float:
        return self._origin + (self._clock() - self._inner_origin) * self.speed

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds / self.speed)
