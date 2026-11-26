"""Modbus TCP 읽기 경로.

이 파일에는 쓰기 호출이 없습니다. observe/live 의 읽기 클라이언트는
ReadOnlyModbusClient 로 감싸며, 쓰기 계열 메서드를 부르면 즉시 예외가 납니다.
"""
from typing import Any, Dict, List

from . import decode
from .acquisition import SampleSource


class WriteBlockedError(RuntimeError):
    """읽기 전용 클라이언트에서 쓰기를 시도함."""


class ModbusReadError(RuntimeError):
    pass


def create_tcp_client(host: str, port: int, timeout_s: float) -> Any:
    """읽기용 pymodbus 클라이언트. 요청 1건당 timeout_s 가 상한이 되도록 재시도를 끕니다."""
    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError as exc:
        raise RuntimeError("pymodbus==3.6.9 를 설치하세요 (pip install -r requirements.txt)") from exc
    return ModbusTcpClient(host, port=int(port), timeout=float(timeout_s), retries=0,
                           retry_on_empty=False, retry_on_invalid=False, reconnect_delay=0)


class ReadOnlyModbusClient:
    """허용 목록에 있는 읽기 메서드만 통과시키는 래퍼."""

    _ALLOWED = frozenset((
        "connect", "close", "connected", "is_socket_open",
        "read_coils", "read_discrete_inputs", "read_holding_registers", "read_input_registers",
    ))

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        lowered = name.lower()
        if "write" in lowered or lowered in ("execute", "send"):
            raise WriteBlockedError("읽기 전용 클라이언트에서 '{}' 호출이 차단되었습니다".format(name))
        if name in self._ALLOWED:
            return getattr(object.__getattribute__(self, "_inner"), name)
        raise AttributeError("ReadOnlyModbusClient 는 '{}' 를 제공하지 않습니다".format(name))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ReadOnlyModbusClient 는 속성을 바꿀 수 없습니다")


def _check(response: Any) -> Any:
    if response is None:
        raise ModbusReadError("응답 없음")
    if isinstance(response, Exception):
        raise ModbusReadError(str(response))
    if response.isError():
        raise ModbusReadError("Modbus 오류 응답: {}".format(response))
    return response


class ModbusSource(SampleSource):
    """채널마다 개별 요청을 보냅니다(주소 연속성을 가정하지 않음)."""

    def __init__(self, client: ReadOnlyModbusClient, unit_id: int) -> None:
        self._client = client
        self._unit = int(unit_id)

    def read_channel(self, name: str, ch_cfg: Dict[str, Any]) -> List[int]:
        count = decode.register_count(ch_cfg["data_type"])
        address = int(ch_cfg["address_zero_based"])
        if ch_cfg["register_type"] == "input":
            resp = self._client.read_input_registers(address, count=count, slave=self._unit)
        elif ch_cfg["register_type"] == "holding":
            resp = self._client.read_holding_registers(address, count=count, slave=self._unit)
        else:
            raise ModbusReadError("register_type 이 holding/input 이 아닙니다")
        regs = list(_check(resp).registers)
        if len(regs) < count:
            raise ModbusReadError("레지스터 {}개 요청, {}개 수신".format(count, len(regs)))
        return regs[:count]

    def read_feedback(self, fb_cfg: Dict[str, Any]) -> List[int]:
        address = int(fb_cfg["address"])
        rtype = fb_cfg["register_type"]
        if rtype == "coil":
            resp = _check(self._client.read_coils(address, count=1, slave=self._unit))
            return [1 if resp.bits[0] else 0]
        if rtype == "discrete_input":
            resp = _check(self._client.read_discrete_inputs(address, count=1, slave=self._unit))
            return [1 if resp.bits[0] else 0]
        if rtype == "holding":
            resp = _check(self._client.read_holding_registers(address, count=1, slave=self._unit))
            return [int(resp.registers[0])]
        if rtype == "input":
            resp = _check(self._client.read_input_registers(address, count=1, slave=self._unit))
            return [int(resp.registers[0])]
        raise ModbusReadError("지원하지 않는 피드백 register_type: {!r}".format(rtype))

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
