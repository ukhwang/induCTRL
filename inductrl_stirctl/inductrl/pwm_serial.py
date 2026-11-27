"""아두이노 우노 PWM 모터 제어 시리얼 모듈 (속도 설정·감속·정지).

이 모듈은 `arduino/stir_pwm_controller/stir_pwm_controller.ino` 와
줄바꿈(\n)으로 끝나는 ASCII 명령으로 대화합니다.

    PING                -> OK PONG
    SET_PCT <0..100>    -> OK PCT <val>
    SET_PWM <0..255>    -> OK PWM <val>
    STOP                -> OK STOPPED   (즉시 PWM 0, Enable 비활성)
    STATUS              -> OK STATUS pwm=.. pct=.. target_pwm=.. target_pct=.. uptime_ms=.. ramp=.. en=..
    HEARTBEAT           -> OK HEARTBEAT (워치독 시계 재설정)
    잘못된 명령/값      -> ERR UNKNOWN_CMD .. / ERR ARG .. / ERR RANGE ..

설계 원칙
- 표준 라이브러리만으로 동작합니다. `pyserial` 은 실제 포트를 열 때만 필요하고,
  테스트와 `--simulate` 는 전송 인터페이스(`SerialTransport`)만으로 돌아갑니다.
- 이 모듈은 모터를 **시작하거나 방향을 바꾸는 코드를 넣지 않았습니다**. 정지(`STOP`)와
  감속은 있지만, 정지 후 다시 올리는 일은 호출하는 쪽이 명시적으로 결정해야 합니다.
- 우노는 USB 포트를 열 때 DTR 로 리셋됩니다. `connect()` 는 기본 2초 기다린 뒤
  PING 을 재시도해 이 리셋 구간을 넘깁니다.

사용 예 (하드웨어)

    python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --pct 60
    python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --ramp 0 70 --step 10 --interval 0.5
    python3 -m inductrl.pwm_serial --port /dev/ttyACM0 --stop

사용 예 (하드웨어 없이)

    python3 -m inductrl.pwm_serial --simulate --pct 60 --json

Python 코드에서

    from inductrl.pwm_serial import PwmController, PyserialTransport

    with PwmController(PyserialTransport("/dev/ttyACM0")) as pwm:
        pwm.connect()
        pwm.set_speed_pct(40)
        pwm.ramp_speed_pct(40, 70, step_pct=10, step_interval_s=0.5)
        pwm.stop()
"""
import abc
import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .actuator import (OUT_ALREADY_ISSUED, OUT_CONNECTION_ERROR, OUT_ERROR, OUT_SUCCESS,
                       OUT_TIMEOUT, StopActuator, StopWriteResult)
from .timeutil import utc_now_iso

DEFAULT_BAUD = 115200
PCT_MIN, PCT_MAX = 0, 100
PWM_MAX = 255
DEFAULT_COMMAND_TIMEOUT_S = 1.0
DEFAULT_RESET_WAIT_S = 2.0
DEFAULT_PING_ATTEMPTS = 5
DEFAULT_PING_TIMEOUT_S = 0.4
DEFAULT_RETRY_INTERVAL_S = 0.3
DEFAULT_RAMP_STEP_PCT = 5
DEFAULT_RAMP_INTERVAL_S = 0.25


# --------------------------------------------------------------------------- #
# 예외
# --------------------------------------------------------------------------- #
class PwmSerialError(Exception):
    """이 모듈이 내는 모든 오류의 상위 형."""


class PwmTransportError(PwmSerialError):
    """포트를 열지 못했거나 바이트를 보내지 못했습니다(명령이 나가지 않음)."""


class PwmTimeoutError(PwmTransportError):
    """명령은 보냈지만 제한 시간 안에 응답이 없었습니다."""


class PwmProtocolError(PwmSerialError):
    """장치 응답을 해석할 수 없습니다."""


class PwmCommandError(PwmProtocolError):
    """장치가 ERR 로 거부했습니다. `line` 에 원문이 들어 있습니다."""

    def __init__(self, message: str, line: str) -> None:
        super().__init__(message)
        self.line = line


# --------------------------------------------------------------------------- #
# 값 검증
# --------------------------------------------------------------------------- #
def _check_int(value: Any, name: str, low: int, high: int) -> int:
    """정수 범위 검사. bool 은 거부하고, 정수값 float(60.0)은 허용합니다."""
    if isinstance(value, bool):
        raise ValueError("{} 는 정수여야 합니다 (bool 불가)".format(name))
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("{} 는 정수여야 합니다 (현재: {!r})".format(name, value))
        value = int(value)
    if not isinstance(value, int):
        raise ValueError("{} 는 정수여야 합니다 (현재: {!r})".format(name, value))
    if not low <= value <= high:
        raise ValueError("{} 는 {}..{} 범위여야 합니다 (현재: {})".format(name, low, high, value))
    return value


def _check_seconds(value: Any, name: str, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} 는 숫자(초)여야 합니다 (현재: {!r})".format(name, value))
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0 or (not allow_zero and seconds == 0):
        raise ValueError("{} 는 0 이상의 유한한 초여야 합니다 (현재: {!r})".format(name, value))
    return seconds


# --------------------------------------------------------------------------- #
# 전송 인터페이스
# --------------------------------------------------------------------------- #
class SerialTransport(abc.ABC):
    """한 줄 단위 ASCII 전송. 테스트에서는 이 인터페이스만 구현하면 됩니다."""

    description = "abstract"

    @abc.abstractmethod
    def open(self) -> None:
        """포트를 엽니다. 이미 열려 있으면 예외."""

    @abc.abstractmethod
    def send_line(self, text: str) -> None:
        """줄바꿈을 붙여 한 줄을 보냅니다(개행 문자는 포함하지 않습니다)."""

    @abc.abstractmethod
    def read_line(self, timeout_s: float) -> Optional[str]:
        """한 줄을 기다립니다. 시간이 지나면 None."""

    @abc.abstractmethod
    def close(self) -> None:
        """포트를 닫습니다. 모터 상태는 바꾸지 않습니다."""

    def __enter__(self) -> "SerialTransport":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _clean_send_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("보낼 명령은 문자열이어야 합니다")
    body = text.strip()
    if not body:
        raise ValueError("빈 명령은 보낼 수 없습니다")
    if "\n" in body or "\r" in body:
        raise ValueError("명령에는 줄바꿈을 넣을 수 없습니다")
    return body


class PyserialTransport(SerialTransport):
    """`pyserial` 을 쓰는 실제 포트 전송. pyserial 이 없으면 열 때 안내와 함께 실패합니다."""

    description = "pyserial"

    def __init__(self, port: str, baud: int = DEFAULT_BAUD, write_timeout_s: float = 1.0,
                 open_delay_s: float = 0.0) -> None:
        if not isinstance(port, str) or not port:
            raise ValueError("시리얼 포트 이름이 필요합니다 (예: /dev/ttyACM0, COM3)")
        self.port = port
        self.baud = _check_int(baud, "baud", 300, 4000000)
        self.write_timeout_s = _check_seconds(write_timeout_s, "write_timeout_s")
        self.open_delay_s = _check_seconds(open_delay_s, "open_delay_s")
        self._serial = None
        self._buffer = b""

    def open(self) -> None:
        try:
            import serial  # noqa: F401  (선택 의존성)
        except ImportError as exc:
            raise PwmTransportError(
                "pyserial 이 설치되어 있지 않습니다 — `python3 -m pip install pyserial` 후 다시 실행하세요"
            ) from exc
        if self._serial is not None:
            raise PwmTransportError("이미 열린 포트입니다: " + self.port)
        try:
            self._serial = serial.Serial(port=self.port, baudrate=self.baud, timeout=0.0,
                                         write_timeout=self.write_timeout_s or None)
        except Exception as exc:  # serial.SerialException 포함
            raise PwmTransportError("포트를 열 수 없습니다: {} ({})".format(self.port, exc)) from exc
        self._buffer = b""
        if self.open_delay_s > 0:
            time.sleep(self.open_delay_s)

    def send_line(self, text: str) -> None:
        body = _clean_send_text(text)
        if self._serial is None:
            raise PwmTransportError("포트가 열려 있지 않습니다")
        data = (body + "\n").encode("ascii")
        try:
            sent = self._serial.write(data)
            self._serial.flush()
        except Exception as exc:
            raise PwmTransportError("전송 실패: {} ({})".format(body, exc)) from exc
        if sent is not None and int(sent) != len(data):
            raise PwmTransportError("일부만 전송됨: {}바이트 중 {}".format(len(data), sent))

    def read_line(self, timeout_s: float) -> Optional[str]:
        if self._serial is None:
            raise PwmTransportError("포트가 열려 있지 않습니다")
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                raw, self._buffer = self._buffer[:index], self._buffer[index + 1:]
                return raw.decode("ascii", "replace").strip()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                waiting = int(getattr(self._serial, "in_waiting", 0) or 0)
                chunk = self._serial.read(waiting if waiting > 0 else 1)
            except Exception as exc:
                raise PwmTransportError("수신 실패: {}".format(exc)) from exc
            if chunk:
                self._buffer += chunk
            else:
                time.sleep(min(0.005, max(remaining, 0.0)))

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self._buffer = b""


class SimulatedTransport(SerialTransport):
    """하드웨어 없이 펌웨어 동작을 흉내내는 전송. 램프는 즉시 적용됩니다."""

    description = "simulated"

    def __init__(self, baud: int = DEFAULT_BAUD, clock: Callable[[], float] = time.monotonic) -> None:
        self.baud = baud
        self._clock = clock
        self._opened_at = 0.0
        self._queue = []  # type: List[str]
        self.is_open = False
        self.pwm = 0
        self.target_pwm = 0
        self.enabled = False
        self.sent = []  # type: List[str]
        self.stop_count = 0

    # --- SerialTransport ---
    def open(self) -> None:
        if self.is_open:
            raise PwmTransportError("이미 열린 시뮬레이터입니다")
        self.is_open = True
        self._opened_at = self._clock()
        self._queue.append("BOOT pwm=0 ready=1")

    def send_line(self, text: str) -> None:
        body = _clean_send_text(text)
        if not self.is_open:
            raise PwmTransportError("시뮬레이터가 열려 있지 않습니다")
        self.sent.append(body)
        self._queue.extend(self._handle(body))

    def read_line(self, timeout_s: float) -> Optional[str]:
        if self._queue:
            return self._queue.pop(0)
        return None

    def close(self) -> None:
        self.is_open = False

    # --- 시험 도우미 ---
    def inject(self, line: str) -> None:
        """장치가 먼저 보내는 줄(EVT 등)을 흉내냅니다."""
        self._queue.append(line)

    def uptime_ms(self) -> int:
        return int(max(0.0, self._clock() - self._opened_at) * 1000)

    # --- 펌웨어 흉내 ---
    def _handle(self, line: str) -> List[str]:
        head, _, arg = line.partition(" ")
        command = head.upper()

        def parse_int() -> Optional[int]:
            text = arg.strip()
            if not text:
                return None
            try:
                return int(text, 10)
            except ValueError:
                return None

        if command == "PING":
            return ["OK PONG"]
        if command == "SET_PCT":
            pct = parse_int()
            if pct is None:
                return ["ERR ARG SET_PCT"]
            if not PCT_MIN <= pct <= PCT_MAX:
                return ["ERR RANGE SET_PCT {}..{}".format(PCT_MIN, PCT_MAX)]
            self.target_pwm = int((pct * PWM_MAX + 50) // 100)
            self.pwm = self.target_pwm
            self.enabled = True
            return ["OK PCT {}".format(pct)]
        if command == "SET_PWM":
            pwm = parse_int()
            if pwm is None:
                return ["ERR ARG SET_PWM"]
            if not 0 <= pwm <= PWM_MAX:
                return ["ERR RANGE SET_PWM 0..{}".format(PWM_MAX)]
            self.target_pwm = pwm
            self.pwm = pwm
            self.enabled = True
            return ["OK PWM {}".format(pwm)]
        if command == "STOP":
            self.pwm = 0
            self.target_pwm = 0
            self.enabled = False
            self.stop_count += 1
            return ["OK STOPPED"]
        if command == "STATUS":
            return ["OK STATUS pwm={} pct={} target_pwm={} target_pct={} uptime_ms={} "
                    "ramp=1 en={}".format(self.pwm, self.pwm_to_pct(self.pwm), self.target_pwm,
                                          self.pwm_to_pct(self.target_pwm), self.uptime_ms(),
                                          1 if self.enabled else 0)]
        if command == "HEARTBEAT":
            return ["OK HEARTBEAT"]
        return ["ERR UNKNOWN_CMD {}".format(command)]

    @staticmethod
    def pwm_to_pct(pwm: int) -> int:
        return int((pwm * PCT_MAX + PWM_MAX // 2) // PWM_MAX)


# --------------------------------------------------------------------------- #
# 상태 응답
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PwmStatus:
    pwm: int
    pct: int
    target_pwm: Optional[int] = None
    target_pct: Optional[int] = None
    uptime_ms: Optional[int] = None
    en: Optional[int] = None
    ramp: Optional[int] = None
    raw: str = ""
    fields: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"pwm": self.pwm, "pct": self.pct, "target_pwm": self.target_pwm,
                "target_pct": self.target_pct, "uptime_ms": self.uptime_ms,
                "en": self.en, "ramp": self.ramp, "raw": self.raw}

    def describe(self) -> str:
        parts = ["pwm={}".format(self.pwm), "pct={}".format(self.pct)]
        if self.target_pwm is not None:
            parts.append("target_pwm={}".format(self.target_pwm))
        if self.target_pct is not None:
            parts.append("target_pct={}".format(self.target_pct))
        if self.uptime_ms is not None:
            parts.append("uptime_ms={}".format(self.uptime_ms))
        if self.en is not None:
            parts.append("en={}".format(self.en))
        return " ".join(parts)

    @classmethod
    def from_line(cls, line: str) -> "PwmStatus":
        text = (line or "").strip()
        if not text.startswith("OK STATUS"):
            raise PwmProtocolError("STATUS 응답이 아닙니다: " + repr(text))
        fields = {}  # type: Dict[str, str]
        for token in text.split()[2:]:
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key] = value
        if "pwm" not in fields:
            raise PwmProtocolError("STATUS 응답에 pwm 값이 없습니다: " + repr(text))

        def as_int(key: str, low: int, high: int, required: bool) -> Optional[int]:
            if key not in fields:
                if required:
                    raise PwmProtocolError("STATUS 응답에 {} 가 없습니다: {!r}".format(key, text))
                return None
            try:
                value = int(fields[key], 10)
            except ValueError as exc:
                raise PwmProtocolError("STATUS {} 값이 정수가 아닙니다: {!r}".format(key, fields[key])) from exc
            if not low <= value <= high:
                raise PwmProtocolError("STATUS {} 값이 범위 밖입니다: {}".format(key, value))
            return value

        pwm = as_int("pwm", 0, PWM_MAX, True)
        pct = as_int("pct", 0, PCT_MAX, False)
        if pct is None:
            pct = SimulatedTransport.pwm_to_pct(int(pwm))
        return cls(pwm=int(pwm), pct=int(pct),
                   target_pwm=as_int("target_pwm", 0, PWM_MAX, False),
                   target_pct=as_int("target_pct", 0, PCT_MAX, False),
                   uptime_ms=as_int("uptime_ms", 0, 2 ** 31 - 1, False),
                   en=as_int("en", 0, 1, False),
                   ramp=as_int("ramp", 0, 1, False),
                   raw=text, fields=fields)


# --------------------------------------------------------------------------- #
# 제어기
# --------------------------------------------------------------------------- #
class PwmController:
    """시리얼 전송 위에서 명령을 주고받는 제어기. 한 줄 = 한 명령."""

    def __init__(self, transport: SerialTransport, *, command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
                 reset_wait_s: float = DEFAULT_RESET_WAIT_S, ping_attempts: int = DEFAULT_PING_ATTEMPTS,
                 ping_timeout_s: float = DEFAULT_PING_TIMEOUT_S,
                 retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not isinstance(transport, SerialTransport):
            raise TypeError("SerialTransport 구현이 필요합니다")
        self.transport = transport
        self.command_timeout_s = _check_seconds(command_timeout_s, "command_timeout_s", allow_zero=False)
        self.reset_wait_s = _check_seconds(reset_wait_s, "reset_wait_s")
        self.ping_attempts = _check_int(ping_attempts, "ping_attempts", 1, 20)
        self.ping_timeout_s = _check_seconds(ping_timeout_s, "ping_timeout_s", allow_zero=False)
        self.retry_interval_s = _check_seconds(retry_interval_s, "retry_interval_s")
        self._sleep = sleep
        self.connected = False
        self.frames_sent = 0
        self.last_response = ""
        self.events = []  # type: List[str]

    # --- 연결 ---
    def connect(self, reset_wait_s: Optional[float] = None) -> "PwmController":
        wait = self.reset_wait_s if reset_wait_s is None else _check_seconds(reset_wait_s, "reset_wait_s")
        self.transport.open()
        self.connected = True
        if wait > 0:
            self._sleep(wait)  # 우노는 포트를 열 때 DTR 로 리셋됩니다.
        last_error = None  # type: Optional[Exception]
        for attempt in range(self.ping_attempts):
            try:
                self.ping(timeout_s=self.ping_timeout_s)
                return self
            except PwmCommandError:
                raise
            except PwmTransportError as exc:
                last_error = exc
            if attempt < self.ping_attempts - 1:
                self._sleep(self.retry_interval_s)
        raise PwmTimeoutError("PING 응답 없음 (포트 {}, {}회 시도)".format(
            getattr(self.transport, "port", self.transport.description), self.ping_attempts)) from last_error

    def close(self) -> None:
        try:
            self.transport.close()
        finally:
            self.connected = False

    def __enter__(self) -> "PwmController":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # --- 내부 교환 ---
    def _exchange(self, command: str, timeout_s: Optional[float] = None) -> str:
        if not self.connected:
            raise PwmTransportError("connect() 를 먼저 호출하세요")
        timeout = self.command_timeout_s if timeout_s is None else _check_seconds(
            timeout_s, "timeout_s", allow_zero=False)
        self.transport.send_line(command)
        self.frames_sent += 1
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PwmTimeoutError("응답 시간 초과: {} ({}초)".format(command, timeout))
            line = self.transport.read_line(remaining)
            if line is None:
                raise PwmTimeoutError("응답 없음: {} ({}초)".format(command, timeout))
            text = line.strip()
            if not text:
                continue
            if text.startswith("ERR"):
                raise PwmCommandError("장치가 명령을 거부했습니다: " + text, text)
            if text.startswith("OK"):
                self.last_response = text
                return text
            # BOOT / EVT / INFO 등 요청하지 않은 줄은 알림으로 보관하고 계속 기다립니다.
            self.events.append(text)

    @staticmethod
    def _ack_value(line: str, key: str) -> int:
        parts = line.split()
        if len(parts) < 3 or parts[0] != "OK" or parts[1] != key:
            raise PwmProtocolError("예상과 다른 응답입니다 (OK {} <값>): {!r}".format(key, line))
        try:
            return int(parts[2], 10)
        except ValueError as exc:
            raise PwmProtocolError("응답 값이 정수가 아닙니다: {!r}".format(line)) from exc

    # --- 명령 ---
    def ping(self, timeout_s: Optional[float] = None) -> bool:
        line = self._exchange("PING", timeout_s)
        if line.split()[:2] != ["OK", "PONG"]:
            raise PwmProtocolError("PING 응답이 아닙니다: {!r}".format(line))
        return True

    def set_speed_pct(self, pct: int, timeout_s: Optional[float] = None) -> int:
        """목표 듀티를 퍼센트로 설정합니다. 장치가 확인한 퍼센트를 돌려줍니다."""
        value = _check_int(pct, "pct", PCT_MIN, PCT_MAX)
        line = self._exchange("SET_PCT {}".format(value), timeout_s)
        acked = self._ack_value(line, "PCT")
        if acked != value:
            raise PwmProtocolError("확인 값이 요청과 다릅니다: 요청 {} / 응답 {!r}".format(value, line))
        return acked

    def set_pwm(self, pwm: int, timeout_s: Optional[float] = None) -> int:
        """목표 듀티를 0~255 로 직접 설정합니다. 장치가 확인한 값을 돌려줍니다."""
        value = _check_int(pwm, "pwm", 0, PWM_MAX)
        line = self._exchange("SET_PWM {}".format(value), timeout_s)
        acked = self._ack_value(line, "PWM")
        if acked != value:
            raise PwmProtocolError("확인 값이 요청과 다릅니다: 요청 {} / 응답 {!r}".format(value, line))
        return acked

    def stop(self, timeout_s: Optional[float] = None) -> str:
        """즉시 정지(STOP). 장치 응답 줄을 돌려줍니다."""
        line = self._exchange("STOP", timeout_s)
        if line.split()[:2] != ["OK", "STOPPED"]:
            raise PwmProtocolError("STOP 응답이 아닙니다: {!r}".format(line))
        return line

    def heartbeat(self, timeout_s: Optional[float] = None) -> bool:
        line = self._exchange("HEARTBEAT", timeout_s)
        if line.split()[:2] != ["OK", "HEARTBEAT"]:
            raise PwmProtocolError("HEARTBEAT 응답이 아닙니다: {!r}".format(line))
        return True

    def status(self, timeout_s: Optional[float] = None) -> PwmStatus:
        line = self._exchange("STATUS", timeout_s)
        return PwmStatus.from_line(line)

    def ramp_speed_pct(self, start_pct: int, target_pct: int, step_pct: int = DEFAULT_RAMP_STEP_PCT,
                       step_interval_s: float = DEFAULT_RAMP_INTERVAL_S,
                       on_step: Optional[Callable[[int, int], None]] = None,
                       timeout_s: Optional[float] = None) -> int:
        """start_pct 에서 target_pct 까지 step_pct 씩 나눠 SET_PCT 를 보냅니다.

        아두이노 자체 램프(RAMP_ENABLED)와 별개로, 소프트웨어에서 단계적으로
        감속/가속하고 싶을 때 씁니다. 마지막으로 확인된 퍼센트를 돌려줍니다.
        """
        start = _check_int(start_pct, "start_pct", PCT_MIN, PCT_MAX)
        target = _check_int(target_pct, "target_pct", PCT_MIN, PCT_MAX)
        step = _check_int(step_pct, "step_pct", 1, PCT_MAX)
        interval = _check_seconds(step_interval_s, "step_interval_s")

        points = [start]
        current = start
        while current != target:
            current = min(target, current + step) if target > current else max(target, current - step)
            points.append(current)

        acked = start
        for index, point in enumerate(points):
            acked = self.set_speed_pct(point, timeout_s)
            if on_step is not None:
                on_step(point, acked)
            if index < len(points) - 1 and interval > 0:
                self._sleep(interval)
        return acked


class PwmStopActuator(StopActuator):
    """`StopActuator` 규격으로 아두이노 STOP 을 보냅니다 (배치당 1회, 재시도 없음).

    `inductrl/actuator.py` 의 Modbus 정지와 **함께** 쓸 수 있습니다. 이 클래스는
    STOP 만 보내며, 정지 후 다시 올리거나 방향을 바꾸는 코드는 없습니다.
    """

    kind = "pwm_serial"

    def __init__(self, controller: PwmController,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._controller = controller
        self._clock = clock
        self._issued = False

    def request_stop(self) -> StopWriteResult:
        if self._issued:
            return StopWriteResult(OUT_ALREADY_ISSUED, "이 배치에서 이미 정지 요청을 보냄 — 전송 안 함",
                                   utc_now_iso(), self._clock(), 0)
        self._issued = True  # 예외가 나도 다시 보내지 않도록 먼저 표시
        before = self._controller.frames_sent
        try:
            line = self._controller.stop()
        except PwmCommandError as exc:
            return StopWriteResult(OUT_ERROR, "STOP 거부됨: {}".format(exc.line), utc_now_iso(),
                                   self._clock(), self._controller.frames_sent - before)
        except PwmTimeoutError as exc:
            return StopWriteResult(OUT_TIMEOUT, "STOP 전송 후 응답 없음: {}".format(exc), utc_now_iso(),
                                   self._clock(), self._controller.frames_sent - before)
        except PwmSerialError as exc:
            return StopWriteResult(OUT_CONNECTION_ERROR, "STOP 전송 실패: {}".format(exc), utc_now_iso(),
                                   self._clock(), self._controller.frames_sent - before)
        return StopWriteResult(OUT_SUCCESS, "아두이노 STOP 응답: " + line, utc_now_iso(),
                               self._clock(), self._controller.frames_sent - before)


# --------------------------------------------------------------------------- #
# 설정 연동
# --------------------------------------------------------------------------- #
def config_section(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """`config.json` 의 선택 항목 `arduino_pwm` 을 기본값과 합쳐 돌려줍니다."""
    section = {
        "enabled": False,
        "port": None,
        "baud": DEFAULT_BAUD,
        "command_timeout_s": DEFAULT_COMMAND_TIMEOUT_S,
        "reset_wait_s": DEFAULT_RESET_WAIT_S,
        "ramp_step_pct": DEFAULT_RAMP_STEP_PCT,
        "ramp_interval_s": DEFAULT_RAMP_INTERVAL_S,
    }  # type: Dict[str, Any]
    block = (cfg or {}).get("arduino_pwm")
    if block is None:
        return section
    if not isinstance(block, dict):
        raise ValueError("arduino_pwm 는 객체여야 합니다 (현재: {!r})".format(type(block).__name__))
    for key, value in block.items():
        if value is not None:
            section[key] = value
    return section


def controller_from_config(cfg: Optional[Dict[str, Any]] = None, *, transport: Optional[SerialTransport] = None,
                           port: Optional[str] = None, baud: Optional[int] = None,
                           command_timeout_s: Optional[float] = None, reset_wait_s: Optional[float] = None,
                           simulate: bool = False, sleep: Callable[[float], None] = time.sleep) -> PwmController:
    """설정·인자로부터 제어기를 만듭니다. 포트가 없으면 ValueError."""
    section = config_section(cfg)
    chosen_port = port if port is not None else section.get("port")
    chosen_baud = _check_int(baud if baud is not None else section.get("baud", DEFAULT_BAUD),
                             "baud", 300, 4000000)
    timeout = section.get("command_timeout_s", DEFAULT_COMMAND_TIMEOUT_S)
    if command_timeout_s is not None:
        timeout = command_timeout_s
    wait = section.get("reset_wait_s", DEFAULT_RESET_WAIT_S)
    if reset_wait_s is not None:
        wait = reset_wait_s

    if transport is None:
        if simulate:
            transport = SimulatedTransport(baud=chosen_baud)
            wait = 0.0  # 시뮬레이터는 리셋이 없습니다.
        elif chosen_port:
            transport = PyserialTransport(chosen_port, baud=chosen_baud)
        else:
            raise ValueError("시리얼 포트를 지정하세요 (--port 또는 config 의 arduino_pwm.port)")
    return PwmController(transport, command_timeout_s=timeout, reset_wait_s=wait, sleep=sleep)


# --------------------------------------------------------------------------- #
# 명령행
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m inductrl.pwm_serial",
        description="아두이노 우노 PWM 모터 제어(stir_pwm_controller) 시리얼 도구")
    parser.add_argument("--port", help="시리얼 포트 (예: /dev/ttyACM0, COM3)")
    parser.add_argument("--baud", type=int, default=None, help="보드율 (기본 115200)")
    parser.add_argument("--config", help="config.json 경로 — arduino_pwm 항목을 읽습니다")
    parser.add_argument("--simulate", action="store_true", help="하드웨어 없이 가상 장치로 실행")
    parser.add_argument("--timeout", type=float, default=None, help="명령 1건 응답 대기(초, 기본 1.0)")
    parser.add_argument("--reset-wait", type=float, default=None, help="포트를 연 뒤 대기(초, 기본 2.0)")
    parser.add_argument("--settle", type=float, default=0.0, help="명령 후 상태를 읽기 전 대기(초, 기본 0)")
    parser.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")

    action = parser.add_mutually_exclusive_group()
    action.add_argument("--ping", action="store_true", help="PING 만")
    action.add_argument("--status", action="store_true", help="상태만 (인자 없을 때 기본)")
    action.add_argument("--pct", type=int, help="목표 듀티 0~100%% 로 설정")
    action.add_argument("--pwm", type=int, help="목표 듀티 0~255 로 직접 설정")
    action.add_argument("--stop", action="store_true", help="즉시 정지(STOP)")
    action.add_argument("--ramp", nargs=2, type=int, metavar=("START_PCT", "END_PCT"),
                        help="START 에서 END 까지 단계적으로 변경")
    parser.add_argument("--step", type=int, default=DEFAULT_RAMP_STEP_PCT, help="램프 단계 폭(%%, 기본 5)")
    parser.add_argument("--interval", type=float, default=DEFAULT_RAMP_INTERVAL_S,
                        help="램프 단계 간격(초, 기본 0.25)")
    return parser


def _load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("설정 파일 최상위는 객체여야 합니다")
    return data


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # 포트를 열기 전에 값부터 검증합니다(잘못된 값으로 모터를 건드리지 않도록).
    if args.pct is not None:
        action = "pct"
    elif args.pwm is not None:
        action = "pwm"
    elif args.ramp is not None:
        action = "ramp"
    elif args.stop:
        action = "stop"
    elif args.ping:
        action = "ping"
    else:
        action = "status"
    try:
        if action == "pct":
            _check_int(args.pct, "--pct", PCT_MIN, PCT_MAX)
        if action == "pwm":
            _check_int(args.pwm, "--pwm", 0, PWM_MAX)
        if action == "ramp":
            _check_int(args.ramp[0], "--ramp START", PCT_MIN, PCT_MAX)
            _check_int(args.ramp[1], "--ramp END", PCT_MIN, PCT_MAX)
            _check_int(args.step, "--step", 1, PCT_MAX)
            _check_seconds(args.interval, "--interval")
        _check_seconds(args.settle, "--settle")
        cfg = _load_config(args.config)
        section = config_section(cfg)
        if args.port is None and not args.simulate and section.get("port") is None:
            raise ValueError("시리얼 포트가 없습니다 — --port /dev/ttyACM0 또는 --simulate")
        controller = controller_from_config(cfg, port=args.port, baud=args.baud,
                                            command_timeout_s=args.timeout,
                                            reset_wait_s=args.reset_wait, simulate=args.simulate)
    except (ValueError, OSError) as exc:
        print("설정 오류: {}".format(exc), file=sys.stderr)
        return 2
    except PwmSerialError as exc:
        print("설정 오류: {}".format(exc), file=sys.stderr)
        return 2

    result = {"action": action, "port": getattr(controller.transport, "port", None),
              "baud": getattr(controller.transport, "baud", None)}  # type: Dict[str, Any]
    try:
        with controller:
            controller.connect()
            if action == "ping":
                controller.ping()
                result["ping"] = "PONG"
            elif action == "pct":
                result["pct"] = controller.set_speed_pct(args.pct)
            elif action == "pwm":
                result["pwm"] = controller.set_pwm(args.pwm)
            elif action == "ramp":
                result["pct"] = controller.ramp_speed_pct(args.ramp[0], args.ramp[1], step_pct=args.step,
                                                          step_interval_s=args.interval)
            elif action == "stop":
                result["stop"] = controller.stop()
            if args.settle > 0:
                time.sleep(args.settle)
            if action != "ping":
                status = controller.status()
                result["status"] = status.as_dict()
    except PwmCommandError as exc:
        print("장치 오류: {}".format(exc), file=sys.stderr)
        return 4
    except PwmSerialError as exc:
        print("통신 오류: {}".format(exc), file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        if action == "ping":
            print("PONG")
        elif action == "pct":
            print("목표 {}% 설정 완료".format(result["pct"]))
        elif action == "pwm":
            print("목표 PWM {} 설정 완료".format(result["pwm"]))
        elif action == "ramp":
            print("램프 완료: {}%".format(result["pct"]))
        elif action == "stop":
            print("정지 완료 ({}), pwm=0".format(result.get("stop", "OK STOPPED")))
        status_line = result.get("status")
        if status_line:
            status = PwmStatus(pwm=status_line["pwm"], pct=status_line["pct"],
                               target_pwm=status_line["target_pwm"], target_pct=status_line["target_pct"],
                               uptime_ms=status_line["uptime_ms"], en=status_line["en"],
                               ramp=status_line["ramp"], raw=status_line["raw"])
            print("상태: " + status.describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
