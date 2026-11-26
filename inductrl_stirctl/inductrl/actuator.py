"""정지 요청 액추에이터. ★ Modbus 쓰기 코드는 이 파일에만 있습니다 ★

허용되는 쓰기는 단 하나: live 모드에서 배치당 1회
    write_coil(stop_coil_address, stop_value, slave=unit_id)
- 자동 재시도 없음 (pymodbus retries=0, retry_on_empty/invalid=False)
- DO 를 원래 값으로 되돌리는 쓰기 없음 (종료·Ctrl+C 포함)
- 모터 시작/재가동 코드 없음
"""
import abc
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .timeutil import utc_now_iso

OUT_SUCCESS = "success"
OUT_EXCEPTION_RESPONSE = "exception_response"
OUT_TIMEOUT = "timeout"
OUT_CONNECTION_ERROR = "connection_error"
OUT_ECHO_MISMATCH = "echo_mismatch"
OUT_ERROR = "error"
OUT_ALREADY_ISSUED = "already_issued"
OUTCOMES = (OUT_SUCCESS, OUT_EXCEPTION_RESPONSE, OUT_TIMEOUT, OUT_CONNECTION_ERROR,
            OUT_ECHO_MISMATCH, OUT_ERROR, OUT_ALREADY_ISSUED)


@dataclass
class StopWriteResult:
    outcome: str
    detail: str
    responded_at_utc: str
    monotonic_s: float
    write_calls: int

    def as_detail(self) -> Dict[str, Any]:
        return {"outcome": self.outcome, "detail": self.detail,
                "responded_at_utc": self.responded_at_utc, "write_calls": self.write_calls}


class StopActuator(abc.ABC):
    kind = "abstract"

    @abc.abstractmethod
    def request_stop(self) -> StopWriteResult:
        """정지 요청을 정확히 1회 보냅니다. 두 번째 호출은 아무것도 쓰지 않습니다."""

    def close(self) -> None:
        return None


class NoWriteActuator(StopActuator):
    """observe 모드: 쓰기 기능 자체가 없습니다."""

    kind = "none"

    def request_stop(self) -> StopWriteResult:
        raise RuntimeError("observe 모드에서는 정지 요청을 보낼 수 없습니다")


class MockActuator(StopActuator):
    """가상 액추에이터: 호출을 기록하고, 설정에 따라 가상 모터를 멈춥니다."""

    kind = "mock"

    def __init__(self, clock: Callable[[], float], outcome: str = OUT_SUCCESS,
                 on_stop: Optional[Callable[[float], None]] = None,
                 stops_motor: bool = True) -> None:
        if outcome not in OUTCOMES:
            raise ValueError("알 수 없는 가상 결과: " + outcome)
        self._clock = clock
        self.outcome = outcome
        self._on_stop = on_stop
        self._stops_motor = stops_motor
        self.calls = []  # type: List[float]
        self._issued = False

    def request_stop(self) -> StopWriteResult:
        if self._issued:
            return StopWriteResult(OUT_ALREADY_ISSUED, "이 배치에서 이미 정지 요청을 보냄",
                                   utc_now_iso(), self._clock(), 0)
        self._issued = True
        t = self._clock()
        self.calls.append(t)
        if self._stops_motor and self._on_stop is not None:
            self._on_stop(t)
        return StopWriteResult(self.outcome, "가상 액추에이터 결과: " + self.outcome,
                               utc_now_iso(), self._clock(), 1)


def create_write_client(host: str, port: int, timeout_s: float) -> Any:
    """쓰기 전용 클라이언트(읽기용과 별도). pymodbus 3.6.9 내부 재시도를 모두 끕니다."""
    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError as exc:
        raise RuntimeError("pymodbus==3.6.9 를 설치하세요") from exc
    client = ModbusTcpClient(host, port=int(port), timeout=float(timeout_s), retries=0,
                             retry_on_empty=False, retry_on_invalid=False,
                             no_resend_on_retry=True, reconnect_delay=0)
    tm = getattr(client, "transaction", None)
    if tm is not None:
        if (getattr(tm, "retries", 0) != 0 or getattr(tm, "retry_on_empty", False)
                or getattr(tm, "retry_on_invalid", False)):
            raise RuntimeError("쓰기 클라이언트 재시도 옵션이 꺼지지 않았습니다 — 실행 중단")
    return client


def _classify_response(resp: Any, address: int, value: bool) -> Tuple[str, str]:
    if resp is None:
        return OUT_TIMEOUT, "응답 없음(None)"
    if isinstance(resp, Exception):
        # pymodbus 3.6.9: 무응답 시 ModbusIOException 객체를 '반환'합니다.
        text = str(resp)
        return OUT_TIMEOUT, "무응답/입출력 오류: " + text
    if getattr(resp, "exception_code", None) is not None:
        return OUT_EXCEPTION_RESPONSE, "Modbus 예외 응답: {}".format(resp)
    try:
        is_err = bool(resp.isError())
    except Exception as exc:
        return OUT_ERROR, "응답 해석 실패: {}".format(exc)
    if is_err:
        return OUT_EXCEPTION_RESPONSE, "오류 응답: {}".format(resp)
    echo_addr = getattr(resp, "address", None)
    echo_val = getattr(resp, "value", None)
    if echo_addr is not None and int(echo_addr) != int(address):
        return OUT_ECHO_MISMATCH, "응답 주소 {} != 요청 주소 {}".format(echo_addr, address)
    if echo_val is not None and bool(echo_val) != bool(value):
        return OUT_ECHO_MISMATCH, "응답 값 {} != 요청 값 {}".format(echo_val, value)
    return OUT_SUCCESS, "정상 응답: {}".format(resp)


class ModbusStopActuator(StopActuator):
    """live 전용. 배치당 write_coil 정확히 1회."""

    kind = "modbus"

    def __init__(self, cfg: Dict[str, Any], clock: Callable[[], float]) -> None:
        act = cfg["actuator"]
        if act.get("stop_coil_address") is None or not isinstance(act.get("stop_value"), bool):
            raise ValueError("stop_coil_address / stop_value 가 확정되지 않았습니다")
        self._address = int(act["stop_coil_address"])
        self._value = bool(act["stop_value"])
        self._unit = int(cfg["unit_id"])
        self._clock = clock
        self._issued = False
        self.write_calls = 0
        # 읽기용과 별도의 클라이언트. 연결은 정지 요청 시점에 맺습니다.
        self._client = create_write_client(cfg["host"], int(cfg["port"]), float(cfg["write_timeout_s"]))

    def request_stop(self) -> StopWriteResult:
        if self._issued:
            return StopWriteResult(OUT_ALREADY_ISSUED, "이 배치에서 이미 정지 요청을 보냄 — 쓰기 안 함",
                                   utc_now_iso(), self._clock(), 0)
        self._issued = True  # 예외가 나도 다시 보내지 않도록 먼저 표시
        try:
            connected = bool(self._client.connect())
        except Exception as exc:
            return StopWriteResult(OUT_CONNECTION_ERROR, "연결 예외: {}: {}".format(type(exc).__name__, exc),
                                   utc_now_iso(), self._clock(), 0)
        if not connected:
            return StopWriteResult(OUT_CONNECTION_ERROR, "ADAM 연결 실패 — 쓰기 전송 안 됨",
                                   utc_now_iso(), self._clock(), 0)
        self.write_calls += 1
        try:
            resp = self._client.write_coil(self._address, self._value, slave=self._unit)
        except Exception as exc:
            name = type(exc).__name__
            outcome = OUT_CONNECTION_ERROR if "Connection" in name else (
                OUT_TIMEOUT if ("Timeout" in name or "IO" in name) else OUT_ERROR)
            return StopWriteResult(outcome, "{}: {}".format(name, exc), utc_now_iso(), self._clock(),
                                   self.write_calls)
        outcome, detail = _classify_response(resp, self._address, self._value)
        return StopWriteResult(outcome, detail, utc_now_iso(), self._clock(), self.write_calls)

    def close(self) -> None:
        # close 는 소켓만 닫습니다. DO 값을 되돌리는 쓰기는 하지 않습니다.
        try:
            self._client.close()
        except Exception:
            pass
