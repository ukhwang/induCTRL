"""mock 모드: 가상 센서·가상 모터·시험 시나리오.

Modbus 클라이언트를 만들지 않습니다. 설정에서 null 인 값은 MOCK_FILL 의 가상값으로만 채우며,
채운 항목은 batches.config_json 의 "_mock_filled" 에 남습니다(실제 설비 값이 아님).
"""
import copy
import math
import random
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import decode
from .acquisition import SampleSource
from .models import (CH_CONDUCTIVITY, CH_ROTATION, CH_TURBIDITY_BOTTOM, CH_TURBIDITY_TOP,
                     MATERIAL_CHANNELS)

# 가상 장치 전용 값. 실제 ADAM 매핑·품질 기준과 무관합니다.
MOCK_FILL = {
    "channels": {
        CH_TURBIDITY_TOP: {"address_zero_based": 0, "register_type": "input", "data_type": "uint16",
                           "word_order": "big", "scale": 0.1, "offset": 0.0, "unit": "NTU(mock)",
                           "valid_min": 0.0, "valid_max": 4000.0},
        CH_TURBIDITY_BOTTOM: {"address_zero_based": 1, "register_type": "input", "data_type": "uint16",
                              "word_order": "big", "scale": 0.1, "offset": 0.0, "unit": "NTU(mock)",
                              "valid_min": 0.0, "valid_max": 4000.0},
        CH_CONDUCTIVITY: {"address_zero_based": 2, "register_type": "input", "data_type": "float32",
                          "word_order": "big", "scale": 1.0, "offset": 0.0, "unit": "mS/cm(mock)",
                          "valid_min": 0.0, "valid_max": 200.0},
        CH_ROTATION: {"address_zero_based": 4, "register_type": "input", "data_type": "uint16",
                      "word_order": "big", "scale": 1.0, "offset": 0.0, "unit": "count",
                      "valid_min": None, "valid_max": None, "pulses_per_rev": 20,
                      "counter_bits": 16, "rpm_window_seconds": 2.0, "max_rpm": 600.0},
    },
    "decision": {
        "running_rpm_min": 100.0,
        "saltwater": {"conductivity_min": 45.0, "conductivity_max": 55.0,
                      "conductivity_max_range": 1.0},
        "caco3": {"turbidity_top_min": 400.0, "turbidity_top_max": 600.0,
                  "turbidity_bottom_min": 400.0, "turbidity_bottom_max": 600.0,
                  "turbidity_max_range": 20.0, "top_bottom_max_diff": 15.0},
    },
    "actuator": {"stop_coil_address": 0, "stop_value": True, "stop_confirm_timeout_s": 10.0,
                 "stopped_rpm_max": 5.0, "stop_hold_seconds": 2.0},
}


def _fill(dst: Dict[str, Any], src: Dict[str, Any], path: str, filled: List[str]) -> None:
    for key, value in src.items():
        here = path + "." + key if path else key
        if isinstance(value, dict):
            if not isinstance(dst.get(key), dict):
                dst[key] = {}
            _fill(dst[key], value, here, filled)
        elif key not in dst or dst[key] is None:
            if value is not None:
                dst[key] = value
                filled.append(here)


def apply_mock_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(cfg)
    rc = out.get("channels", {}).get(CH_ROTATION, {})
    filled = []  # type: List[str]
    if isinstance(rc, dict) and rc.get("data_type") is None and rc.get("counter_bits") in (16, 32):
        rc["data_type"] = "uint16" if rc["counter_bits"] == 16 else "uint32"
        filled.append("channels.rotation_counter.data_type")
    _fill(out, MOCK_FILL, "", filled)
    out["_mock_filled"] = filled
    return out


# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    profile: str = "converge"          # converge | flat | outside
    tau_s: float = 12.0
    fail_every: int = 0                # 판정 채널 읽기가 N번에 1번 실패
    stale_after_s: Optional[float] = None  # 이 시각 이후 판정 채널 수신 끊김
    fault_every: int = 0               # 판정 채널이 N번에 1번 센서 고장값 출력
    counter_start: str = "zero"        # zero | near_wrap
    counter_reset_at_s: Optional[float] = None
    actuator_outcome: str = "success"
    motor_stops: bool = True           # 정지 요청 시 가상 모터가 실제로 멈추는지
    read_latency_s: float = 0.0


SCENARIOS = {s.name: s for s in (
    Scenario("normal", "정상 완료: 수렴 → 후보 → 정지 요청 → 정지 확인"),
    Scenario("early_flat", "교반 초반부터 평탄·범위 안 → min_stir 전에는 후보 없음", profile="flat"),
    Scenario("out_of_range", "목표 범위 밖에서 안정 → 후보 없음", profile="outside"),
    Scenario("dropout", "판정 채널 4회 중 1회 수신 누락 → coverage 부족, 판정 보류", fail_every=4),
    Scenario("stale", "20초 이후 판정 채널 수신 끊김 → READ_ERROR 후 STALE", stale_after_s=20.0),
    Scenario("invalid_value", "판정 채널 5회 중 1회 센서 고장값 → OUT_OF_RANGE", fault_every=5),
    Scenario("counter_overflow", "회전 카운터가 래핑(오버플로) → RPM 정상 유지", counter_start="near_wrap"),
    Scenario("counter_reset", "20초에 카운터 리셋 → COUNTER_RESET, 구간 동안 RPM 무효",
             counter_reset_at_s=20.0),
    Scenario("write_exception", "정지 쓰기에 Modbus 예외 응답 → stop_unconfirmed",
             actuator_outcome="exception_response", motor_stops=False),
    Scenario("write_timeout", "정지 쓰기 무응답(모터는 실제로 멈춤) → stop_unconfirmed + stop_observed",
             actuator_outcome="timeout", motor_stops=True),
    Scenario("write_connection_error", "정지 쓰기 연결 오류 → stop_unconfirmed",
             actuator_outcome="connection_error", motor_stops=False),
    Scenario("no_rpm_drop", "쓰기 응답은 성공이나 RPM 이 떨어지지 않음 → 타임아웃 후 stop_unconfirmed",
             actuator_outcome="success", motor_stops=False),
    Scenario("slow_reads", "채널당 0.15초 지연 → 주기 예산 초과, SKIPPED_BUDGET", read_latency_s=0.15),
)}


class MockReadError(RuntimeError):
    pass


class MockPlant:
    MOTOR_START_S = 1.0
    RAMP_S = 2.0
    TARGET_RPM = 300.0
    STOP_DECAY_S = 2.0

    def __init__(self, cfg: Dict[str, Any], material: str, scenario: Scenario,
                 clock: Callable[[], float], seed: int = 7) -> None:
        self.cfg = cfg
        self.scenario = scenario
        self.clock = clock
        self.t_origin = clock()
        self.rng = random.Random(seed)
        self.required = MATERIAL_CHANNELS[material]
        rc = cfg["channels"][CH_ROTATION]
        self.ppr = float(rc["pulses_per_rev"])
        self.modulus = 2 ** int(rc["counter_bits"])
        self.count_offset = 0 if scenario.counter_start == "zero" else self.modulus - 2000
        self._revs = 0.0
        self._last_t = None  # type: Optional[float]
        self._reset_done = False
        self.stopped_at = None  # type: Optional[float]
        self._rpm_at_stop = 0.0
        self._reads = {}  # type: Dict[str, int]

    def tau(self, t: float) -> float:
        return t - self.t_origin

    def rpm_at(self, t: float) -> float:
        tau = self.tau(t)
        if tau < self.MOTOR_START_S:
            base = 0.0
        else:
            base = min(self.TARGET_RPM, self.TARGET_RPM * (tau - self.MOTOR_START_S) / self.RAMP_S)
        if self.stopped_at is not None and t >= self.stopped_at:
            return max(0.0, self._rpm_at_stop * (1.0 - (t - self.stopped_at) / self.STOP_DECAY_S))
        return base

    def stop(self, t: float) -> None:
        if self.stopped_at is None:
            self._rpm_at_stop = self.rpm_at(t)
            self.stopped_at = t

    def counter_value(self, t: float) -> int:
        if self._last_t is not None and t > self._last_t:
            self._revs += (self.rpm_at(self._last_t) + self.rpm_at(t)) / 2.0 * (t - self._last_t) / 60.0
        if self._last_t is None or t > self._last_t:
            self._last_t = t
        pulses = int(math.floor(self._revs * self.ppr))
        if (self.scenario.counter_reset_at_s is not None and not self._reset_done
                and self.tau(t) >= self.scenario.counter_reset_at_s):
            self._reset_done = True
            self.count_offset = -pulses
        return (self.count_offset + pulses) % self.modulus

    def _band(self, ch: str) -> Tuple[float, float, Optional[float]]:
        dec = self.cfg["decision"]
        if ch == CH_CONDUCTIVITY:
            b = dec["saltwater"]
            return b["conductivity_min"], b["conductivity_max"], b.get("conductivity_max_range")
        b = dec["caco3"]
        if ch == CH_TURBIDITY_TOP:
            return b["turbidity_top_min"], b["turbidity_top_max"], b.get("turbidity_max_range")
        return b["turbidity_bottom_min"], b["turbidity_bottom_max"], b.get("turbidity_max_range")

    def analog_value(self, ch: str, t: float) -> float:
        """가상 공학값. 판정 채널의 실패 모드는 여기서 발생합니다."""
        n = self._reads.get(ch, 0) + 1
        self._reads[ch] = n
        sc = self.scenario
        if ch in self.required:
            if sc.fail_every and n % sc.fail_every == 0:
                raise MockReadError("가상 수신 누락(시나리오 {})".format(sc.name))
            if sc.stale_after_s is not None and self.tau(t) >= sc.stale_after_s:
                raise MockReadError("가상 통신 끊김(시나리오 {})".format(sc.name))
            if sc.fault_every and n % sc.fault_every == 0:
                ch_cfg = self.cfg["channels"][ch]
                vmin = ch_cfg.get("valid_min") or 0.0
                vmax = ch_cfg.get("valid_max") if ch_cfg.get("valid_max") is not None else 1000.0
                return vmax + (vmax - vmin)
        lo, hi, span = self._band(ch)
        mid, width = (lo + hi) / 2.0, (hi - lo)
        noise_amp = 0.05 * span if span else 0.01 * width
        noise = self.rng.uniform(-1.0, 1.0) * noise_amp
        sign = -1.0 if ch == CH_TURBIDITY_BOTTOM else 1.0
        tm = max(0.0, self.tau(t) - self.MOTOR_START_S)
        if sc.profile == "flat":
            return mid + noise
        swing = sign * 1.5 * width * math.exp(-tm / sc.tau_s) * math.cos(0.7 * tm)
        if sc.profile == "outside":
            return hi + 0.5 * width + swing + noise
        return mid + swing + noise


class MockSource(SampleSource):
    def __init__(self, plant: MockPlant, clock: Callable[[], float],
                 sleep: Callable[[float], None]) -> None:
        self.plant = plant
        self.clock = clock
        self.sleep = sleep

    def read_channel(self, name: str, ch_cfg: Dict[str, Any]) -> List[int]:
        if self.plant.scenario.read_latency_s > 0:
            self.sleep(self.plant.scenario.read_latency_s)
        t = self.clock()
        if name == CH_ROTATION:
            return decode.encode_registers(self.plant.counter_value(t), ch_cfg["data_type"],
                                           ch_cfg.get("word_order"))
        value = self.plant.analog_value(name, t)
        raw = decode.engineering_to_raw(value, ch_cfg["data_type"], ch_cfg["scale"], ch_cfg["offset"])
        return decode.encode_registers(raw, ch_cfg["data_type"], ch_cfg.get("word_order"))

    def read_feedback(self, fb_cfg: Dict[str, Any]) -> List[int]:
        stopped = self.plant.stopped_at is not None and self.plant.rpm_at(self.clock()) == 0.0
        return [1 if stopped else 0]
