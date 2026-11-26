"""원시 샘플 → 유효성 판정 → RPM → 완료 후보.

실행(run)과 재생(replay)이 같은 클래스를 씁니다. 입력이 같으면 결과도 같습니다.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import decode
from .decision import DecisionEngine, DecisionResult
from .models import (ANALOG_CHANNELS, CH_ROTATION, CH_RPM, CH_STOP_FEEDBACK, ST_DECODE_ERROR,
                     ST_OK, ST_OUT_OF_RANGE, ST_RPM_UNCONFIGURED, ST_SKIPPED_BUDGET, ST_STALE,
                     ST_UNMAPPED,
                     EventRecord, RawSample, Reading)
from .rpm import RpmCalculator

EPS = 1e-6


@dataclass
class CycleResult:
    seq: int
    t_eval: float
    readings: List[Reading]
    events: List[EventRecord]
    decision: DecisionResult
    rpm: Optional[float]
    rpm_status: str
    feedback_value: Optional[float]
    candidate_event: Optional[EventRecord] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class BatchProcessor:
    def __init__(self, cfg: Dict[str, Any], material: str, start_trigger: str,
                 run_started_t: float) -> None:
        self.cfg = cfg
        self.run_started_t = float(run_started_t)
        self.channels = cfg["channels"]
        self.decision = DecisionEngine(cfg, material, start_trigger,
                                       self.run_started_t if start_trigger == "launch" else None)
        self._last_ok = {}  # type: Dict[str, float]
        rc = self.channels[CH_ROTATION]
        self.rpm_calc = None  # type: Optional[RpmCalculator]
        if all(rc.get(k) is not None for k in ("pulses_per_rev", "counter_bits",
                                                 "rpm_window_seconds", "max_rpm")):
            self.rpm_calc = RpmCalculator(
                rc["pulses_per_rev"], int(rc["counter_bits"]), rc["rpm_window_seconds"],
                rc["max_rpm"], max_endpoint_gap_s=2.0 * float(cfg["decision"]["interval_seconds"]))

    # ------------------------------------------------------------------ #
    def process(self, seq: int, raws: List[RawSample]) -> CycleResult:
        t_eval = max(r.monotonic_s for r in raws)
        readings = []  # type: List[Reading]
        events = []  # type: List[EventRecord]
        by_channel = {}  # type: Dict[str, Reading]
        rpm_value = None  # type: Optional[float]
        rpm_status = ST_RPM_UNCONFIGURED
        feedback_value = None  # type: Optional[float]

        for raw in raws:
            if raw.channel == CH_STOP_FEEDBACK:
                reading = self._feedback_reading(raw)
                if reading.status == ST_OK:
                    feedback_value = reading.value
                readings.append(reading)
                continue
            ch_cfg = self.channels[raw.channel]
            reading = self._channel_reading(raw, ch_cfg)
            readings.append(reading)
            by_channel[raw.channel] = reading
            if raw.channel == CH_ROTATION:
                rpm_reading, ev = self._rpm_reading(raw, reading)
                rpm_value = rpm_reading.value
                rpm_status = rpm_reading.status
                readings.append(rpm_reading)
                if ev is not None:
                    events.append(ev)

        decision = self.decision.update(t_eval, rpm_value, by_channel)
        events.extend(decision.events)
        candidate_event = None
        for ev in decision.events:
            if ev.event_type == "candidate":
                candidate_event = ev
        return CycleResult(seq, t_eval, readings, events, decision, rpm_value, rpm_status,
                           feedback_value, candidate_event)

    # ------------------------------------------------------------------ #
    def _invalid(self, raw: RawSample, ch_cfg: Dict[str, Any], status: str,
                 detail: Optional[str]) -> Reading:
        """무효 샘플: 값은 NULL. 마지막 정상 수신 후 stale_seconds 가 지나면 STALE.

        UNMAPPED 와 SKIPPED_BUDGET 은 원인을 그대로 남깁니다(오래됨 정보는 error_detail 에 추가).
        """
        if status != ST_UNMAPPED:
            ref = self._last_ok.get(raw.channel, self.run_started_t)
            age = raw.monotonic_s - ref
            if age > float(ch_cfg["stale_seconds"]) + EPS:
                note = "마지막 정상 수신 후 {:.3f}s".format(age)
                if status == ST_SKIPPED_BUDGET:
                    detail = "{}; {}".format(detail or "", note)
                else:
                    detail = "{} ({}; {})".format(status, detail or "", note)
                    status = ST_STALE
        raw_json = json.dumps(raw.registers) if raw.registers is not None else None
        return Reading(raw.channel, raw.monotonic_s, raw.collected_at_utc, raw_json, None,
                       ch_cfg.get("unit"), status, detail)

    def _channel_reading(self, raw: RawSample, ch_cfg: Dict[str, Any]) -> Reading:
        if raw.registers is None:
            return self._invalid(raw, ch_cfg, raw.read_status or "READ_ERROR", raw.error_detail)
        try:
            decoded = decode.decode_registers(raw.registers, ch_cfg["data_type"], ch_cfg.get("word_order"))
            if raw.channel in ANALOG_CHANNELS:
                value = decode.to_engineering(decoded, ch_cfg["scale"], ch_cfg["offset"])
            else:
                value = float(decoded)  # 회전 카운터 원시 카운트(환산 없음)
        except (decode.DecodeError, TypeError, KeyError) as exc:
            return self._invalid(raw, ch_cfg, ST_DECODE_ERROR, str(exc))
        vmin, vmax = ch_cfg.get("valid_min"), ch_cfg.get("valid_max")
        if (vmin is not None and value < vmin) or (vmax is not None and value > vmax):
            return self._invalid(raw, ch_cfg, ST_OUT_OF_RANGE,
                                 "값 {} 가 유효범위 [{}, {}] 밖".format(value, vmin, vmax))
        self._last_ok[raw.channel] = raw.monotonic_s
        return Reading(raw.channel, raw.monotonic_s, raw.collected_at_utc,
                       json.dumps(raw.registers), value, ch_cfg.get("unit"), ST_OK, None)

    def _rpm_reading(self, raw: RawSample, counter: Reading):
        if self.rpm_calc is None:
            return (Reading(CH_RPM, raw.monotonic_s, raw.collected_at_utc, None, None, "rpm",
                            ST_RPM_UNCONFIGURED, "pulses_per_rev/counter_bits/rpm_window_seconds/max_rpm 미설정"),
                    None)
        count = int(counter.value) if (counter.status == ST_OK and counter.value is not None) else None
        res = self.rpm_calc.update(raw.monotonic_s, count)
        return (Reading(CH_RPM, raw.monotonic_s, raw.collected_at_utc, None, res.value, "rpm",
                        res.status, res.detail), res.event)

    def _feedback_reading(self, raw: RawSample) -> Reading:
        raw_json = json.dumps(raw.registers) if raw.registers is not None else None
        if raw.registers is None or not raw.registers:
            return Reading(CH_STOP_FEEDBACK, raw.monotonic_s, raw.collected_at_utc, raw_json, None,
                           None, raw.read_status or "READ_ERROR", raw.error_detail)
        return Reading(CH_STOP_FEEDBACK, raw.monotonic_s, raw.collected_at_utc, raw_json,
                       float(raw.registers[0]), None, ST_OK, None)
