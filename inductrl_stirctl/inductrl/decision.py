"""완료 후보 판정.

판정은 '변화율 0'이 아니라 구간 내 범위·변동폭 조건으로 합니다.
모든 조건이 hold_seconds 동안 연속 유지될 때 배치당 첫 후보를 한 번만 기록합니다.
"""
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from . import config as config_mod
from .models import (CH_CONDUCTIVITY, CH_TURBIDITY_BOTTOM, CH_TURBIDITY_TOP, EV_BATCH_STARTED,
                     EV_CANDIDATE, EV_HOLD_RESET, MATERIAL_CHANNELS, ST_OK, ST_UNMAPPED,
                     EventRecord, Reading)

EPS = 1e-6

S_WAITING_START = "WAITING_START"
S_BLOCKED = "BLOCKED"
S_MIN_STIR = "MIN_STIR"
S_NOT_MET = "NOT_MET"
S_HOLDING = "HOLDING"
S_CANDIDATE = "CANDIDATE"
S_MET_AFTER_CANDIDATE = "MET_AFTER_CANDIDATE"


@dataclass
class DecisionResult:
    state: str
    reasons: List[str]
    hold_s: Optional[float]
    verified: bool
    events: List[EventRecord] = field(default_factory=list)
    candidate: bool = False


def _stats(values: List[float]) -> Dict[str, float]:
    return {"n": len(values), "min": min(values), "max": max(values),
            "mean": sum(values) / len(values)}


class DecisionEngine:
    def __init__(self, cfg: Dict[str, Any], material: str, start_trigger: str,
                 launch_t: Optional[float]) -> None:
        dec = cfg["decision"]
        self.material = material
        self.start_trigger = start_trigger
        self.launch_t = launch_t
        self.interval = float(dec["interval_seconds"])
        self.window = float(dec["window_seconds"])
        self.min_stir = float(dec["min_stir_seconds"])
        self.hold = float(dec["hold_seconds"])
        self.min_coverage = float(dec["min_coverage"])
        self.running_min = dec.get("running_rpm_min")
        self.confirm = float(dec["running_confirm_seconds"])
        self.thr = dict(dec[material])
        self.channels = MATERIAL_CHANNELS[material]
        self.stale = {ch: float(cfg["channels"][ch]["stale_seconds"]) for ch in self.channels}
        self.unverified_reasons = config_mod.unverified_reasons(cfg, material)
        self.verified = not self.unverified_reasons
        self.required_samples = self.window / self.interval * self.min_coverage
        self._win = {ch: deque() for ch in self.channels}  # type: Dict[str, Deque[Tuple[float, Optional[float]]]]
        self._last_ok = {ch: None for ch in self.channels}  # type: Dict[str, Optional[float]]
        self.t0 = None  # type: Optional[float]
        self._running_since = None  # type: Optional[float]
        self._hold_start = None  # type: Optional[float]
        self.candidate_t = None  # type: Optional[float]

    # ------------------------------------------------------------------ #
    def update(self, t: float, rpm: Optional[float], current: Dict[str, Reading]) -> DecisionResult:
        events = []  # type: List[EventRecord]
        for ch in self.channels:
            r = current.get(ch)
            if r is None:
                continue
            ok = r.status == ST_OK and r.value is not None
            self._win[ch].append((r.monotonic_s, r.value if ok else None))
            if ok:
                self._last_ok[ch] = r.monotonic_s
            while self._win[ch] and self._win[ch][0][0] < t - self.window - 5.0:
                self._win[ch].popleft()

        if self.t0 is None:
            started = self._try_start(t, rpm)
            if started is not None:
                events.append(started)
            if self.t0 is None:
                reasons = ["running_rpm_min_unset"] if (
                    self.start_trigger == "rpm" and self.running_min is None) else ["waiting_start"]
                state = S_BLOCKED if reasons[0] == "running_rpm_min_unset" else S_WAITING_START
                return DecisionResult(state, reasons, None, self.verified, events)

        reasons, stats = self._evaluate(t, rpm, current)
        met = not reasons
        candidate = False
        hold_s = None  # type: Optional[float]
        if met:
            if self._hold_start is None:
                self._hold_start = t
            hold_s = t - self._hold_start
            if self.candidate_t is not None:
                state = S_MET_AFTER_CANDIDATE
            elif hold_s + EPS >= self.hold:
                self.candidate_t = t
                candidate = True
                state = S_CANDIDATE
                events.append(EventRecord(EV_CANDIDATE, t, {
                    "verified": self.verified,
                    "label": "검증된 판정" if self.verified else "미검증 판정",
                    "unverified_reasons": self.unverified_reasons,
                    "material": self.material,
                    "since_t0_s": round(t - self.t0, 6),
                    "hold_s": round(hold_s, 6),
                    "rpm": rpm,
                    "window_stats": stats,
                }))
            else:
                state = S_HOLDING
        else:
            if self._hold_start is not None and self.candidate_t is None:
                events.append(EventRecord(EV_HOLD_RESET, t, {
                    "held_s": round(t - self._hold_start, 6), "reasons": reasons}))
            self._hold_start = None
            if any(r.startswith("unmapped:") or r == "running_rpm_min_unset" for r in reasons):
                state = S_BLOCKED
            elif "min_stir" in reasons:
                state = S_MIN_STIR
            else:
                state = S_NOT_MET
        return DecisionResult(state, reasons, hold_s, self.verified, events, candidate)

    # ------------------------------------------------------------------ #
    def _try_start(self, t: float, rpm: Optional[float]) -> Optional[EventRecord]:
        if self.start_trigger == "launch":
            self.t0 = float(self.launch_t if self.launch_t is not None else t)
            return EventRecord(EV_BATCH_STARTED, self.t0, {"trigger": "launch"})
        if self.running_min is None:
            return None
        if rpm is not None and rpm >= float(self.running_min):
            if self._running_since is None:
                self._running_since = t
            if t - self._running_since + EPS >= self.confirm:
                self.t0 = t
                return EventRecord(EV_BATCH_STARTED, t, {
                    "trigger": "rpm", "running_since_monotonic_s": self._running_since,
                    "rpm": rpm, "running_rpm_min": self.running_min})
        else:
            self._running_since = None
        return None

    def _evaluate(self, t: float, rpm: Optional[float],
                  current: Dict[str, Reading]) -> Tuple[List[str], Dict[str, Any]]:
        assert self.t0 is not None
        reasons = []  # type: List[str]
        if self.running_min is None:
            reasons.append("running_rpm_min_unset")
        if t - self.t0 + EPS < self.min_stir:
            reasons.append("min_stir")
        if rpm is None:
            reasons.append("rpm_invalid")
        elif self.running_min is not None and rpm < float(self.running_min):
            reasons.append("not_running")
        if t - self.window + EPS < self.t0:
            reasons.append("window_not_filled")

        values = {}  # type: Dict[str, List[float]]
        for ch in self.channels:
            r = current.get(ch)
            if r is not None and r.status == ST_UNMAPPED:
                reasons.append("unmapped:" + ch)
                continue
            if r is None or r.status != ST_OK or r.value is None:
                reasons.append("current_invalid:" + ch)
            vals = [v for (ts, v) in self._win[ch] if t - self.window + EPS < ts <= t + EPS and v is not None]
            values[ch] = vals
            if len(vals) + EPS < self.required_samples:
                reasons.append("coverage:{}({}/{:.1f})".format(ch, len(vals), self.required_samples))
            last = self._last_ok[ch]
            if last is None or t - last > self.stale[ch] + EPS:
                reasons.append("stale:" + ch)

        stats = {ch: _stats(v) for ch, v in values.items() if v}
        reasons.extend(self._threshold_failures(values))
        return reasons, stats

    def _threshold_failures(self, values: Dict[str, List[float]]) -> List[str]:
        """null 임계값은 적용하지 않습니다(그 경우 후보는 '미검증 판정'으로 표시)."""
        th = self.thr
        failed = []  # type: List[str]

        def check(ch: str, vals: List[float], lo_key: str, hi_key: str, range_key: str) -> None:
            lo, hi, span = th.get(lo_key), th.get(hi_key), th.get(range_key)
            if lo is not None and min(vals) < lo:
                failed.append("below_min:" + ch)
            if hi is not None and max(vals) > hi:
                failed.append("above_max:" + ch)
            if span is not None and max(vals) - min(vals) > span + 1e-12:
                failed.append("range:" + ch)

        if self.material == "saltwater":
            vals = values.get(CH_CONDUCTIVITY) or []
            if vals:
                check(CH_CONDUCTIVITY, vals, "conductivity_min", "conductivity_max",
                      "conductivity_max_range")
        else:
            top = values.get(CH_TURBIDITY_TOP) or []
            bot = values.get(CH_TURBIDITY_BOTTOM) or []
            if top:
                check(CH_TURBIDITY_TOP, top, "turbidity_top_min", "turbidity_top_max",
                      "turbidity_max_range")
            if bot:
                check(CH_TURBIDITY_BOTTOM, bot, "turbidity_bottom_min", "turbidity_bottom_max",
                      "turbidity_max_range")
            diff = th.get("top_bottom_max_diff")
            if top and bot and diff is not None:
                if abs(sum(top) / len(top) - sum(bot) / len(bot)) > diff + 1e-12:
                    failed.append("top_bottom_diff")
        return failed
