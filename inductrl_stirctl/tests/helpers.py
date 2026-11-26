"""테스트 공용 도우미. 하드웨어·네트워크 없이 동작합니다."""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

from inductrl import db as db_mod
from inductrl.mock import SCENARIOS, MockPlant, MockSource, apply_mock_defaults
from inductrl.runner import BatchRunner, RunOptions, RunSummary
from inductrl.timeutil import SimClock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_CONFIG = os.path.join(ROOT, "config.example.json")


def example_cfg() -> Dict[str, Any]:
    with open(EXAMPLE_CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)


def verified_cfg() -> Dict[str, Any]:
    """모든 값이 채워지고 검증 플래그가 켜진 '가상 현장' 설정 (테스트 전용)."""
    cfg = apply_mock_defaults(example_cfg())
    cfg.pop("_mock_filled", None)
    for ch in cfg["channels"].values():
        ch["mapping_verified"] = True
    cfg["decision"]["thresholds_verified"] = True
    cfg["actuator"]["live_enabled"] = True
    cfg["actuator"]["verified"] = True
    return cfg


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="inductrl-test-")
        self.db_path = os.path.join(self.tmp, "test.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def run_mock(self, material: str = "saltwater", scenario: str = "normal", samples: int = 200,
                 batch_id: Optional[str] = None, start_trigger: str = "rpm",
                 cfg: Optional[Dict[str, Any]] = None) -> RunSummary:
        opts = RunOptions(mode="mock", material=material, batch_id=batch_id or "b-" + scenario,
                          start_trigger=start_trigger, samples=samples, scenario=scenario, fast=True,
                          db_path=self.db_path, status_every=0)
        runner = BatchRunner(cfg if cfg is not None else example_cfg(), opts,
                             out=io.StringIO(), err=io.StringIO())
        return runner.run()

    def con(self) -> sqlite3.Connection:
        return db_mod.connect(self.db_path)

    def events(self, batch_id: str) -> List[Dict[str, Any]]:
        con = self.con()
        try:
            return db_mod.list_events(con, batch_id)
        finally:
            con.close()

    def event_types(self, batch_id: str) -> List[str]:
        return [e["event_type"] for e in self.events(batch_id)]

    def first_event(self, batch_id: str, event_type: str) -> Optional[Dict[str, Any]]:
        for e in self.events(batch_id):
            if e["event_type"] == event_type:
                return e
        return None

    def rows(self, sql: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        con = self.con()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# 가짜 pymodbus 클라이언트 (observe / live 경로 시험용)
# --------------------------------------------------------------------------- #
class FakeRegResponse:
    def __init__(self, registers: List[int]) -> None:
        self.registers = registers

    def isError(self) -> bool:
        return False


class FakeBitResponse:
    def __init__(self, bits: List[bool]) -> None:
        self.bits = bits

    def isError(self) -> bool:
        return False


class FakeWriteCoilResponse:
    def __init__(self, address: int, value: bool) -> None:
        self.address = address
        self.value = value

    def isError(self) -> bool:
        return False


class FakeExceptionResponse:
    exception_code = 4

    def isError(self) -> bool:
        return True

    def __str__(self) -> str:
        return "ExceptionResponse(fc=0x85, code=4 SlaveFailure)"


class ModbusIOException(Exception):
    """pymodbus 의 ModbusIOException 을 흉내(무응답 시 '반환'되는 객체)."""


class ConnectionException(Exception):
    pass


class FakeReadClient:
    """가상 플랜트 값을 Modbus 레지스터로 돌려주는 읽기 클라이언트. 쓰기 호출 횟수를 셉니다."""

    def __init__(self, plant: MockPlant, cfg: Dict[str, Any], clock: Any) -> None:
        self.source = MockSource(plant, clock, lambda s: None)
        self.cfg = cfg
        self.write_calls = 0
        self.read_calls = 0

    def _channel_at(self, address: int, register_type: str) -> Tuple[str, Dict[str, Any]]:
        for name, ch in self.cfg["channels"].items():
            if ch["address_zero_based"] == address and ch["register_type"] == register_type:
                return name, ch
        raise AssertionError("매핑 안 된 주소 {}".format(address))

    def connect(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def read_input_registers(self, address: int, count: int = 1, slave: int = 0) -> FakeRegResponse:
        self.read_calls += 1
        name, ch = self._channel_at(address, "input")
        return FakeRegResponse(self.source.read_channel(name, ch))

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 0) -> FakeRegResponse:
        self.read_calls += 1
        name, ch = self._channel_at(address, "holding")
        return FakeRegResponse(self.source.read_channel(name, ch))

    def read_coils(self, address: int, count: int = 1, slave: int = 0) -> FakeBitResponse:
        return FakeBitResponse([bool(self.source.read_feedback({})[0])])

    def read_discrete_inputs(self, address: int, count: int = 1, slave: int = 0) -> FakeBitResponse:
        return FakeBitResponse([bool(self.source.read_feedback({})[0])])

    def write_coil(self, address: int, value: bool, slave: int = 0) -> Any:
        self.write_calls += 1
        raise AssertionError("읽기 클라이언트로 쓰기가 호출됨")

    def write_register(self, address: int, value: int, slave: int = 0) -> Any:
        self.write_calls += 1
        raise AssertionError("읽기 클라이언트로 쓰기가 호출됨")


class FakeWriteClient:
    """behavior: success | exception | io_return | raise_conn | raise_io | connect_fail | echo_mismatch"""

    def __init__(self, behavior: str = "success", plant: Optional[MockPlant] = None,
                 clock: Any = None) -> None:
        self.behavior = behavior
        self.plant = plant
        self.clock = clock
        self.write_calls = []  # type: List[Tuple[int, bool, int]]
        self.connect_calls = 0
        self.closed = False

    def connect(self) -> bool:
        self.connect_calls += 1
        return self.behavior != "connect_fail"

    def close(self) -> None:
        self.closed = True

    def write_coil(self, address: int, value: bool, slave: int = 0) -> Any:
        self.write_calls.append((address, value, slave))
        if self.behavior == "raise_conn":
            raise ConnectionException("Connection reset by peer")
        if self.behavior == "raise_io":
            raise ModbusIOException("No response received")
        if self.behavior == "io_return":
            return ModbusIOException("No response received, expected at least 8 bytes")
        if self.behavior == "exception":
            return FakeExceptionResponse()
        if self.plant is not None and self.clock is not None:
            self.plant.stop(self.clock())
        if self.behavior == "echo_mismatch":
            return FakeWriteCoilResponse(address + 1, value)
        return FakeWriteCoilResponse(address, value)


def make_plant(cfg: Dict[str, Any], material: str, clock: SimClock, scenario: str = "normal") -> MockPlant:
    return MockPlant(cfg, material, SCENARIOS[scenario], clock.now)
