"""회전 카운터 → RPM.

- 증가량 delta = (new - old) mod 2^counter_bits (오버플로 처리)
- delta 가 max_rpm 과 경과시간으로 허용되는 최대 펄스 수를 넘으면 리셋/이상으로 판단
- 그 지점이 rpm_window_seconds 구간 안에 있는 동안 RPM 은 무효
- RPM = 구간 내 누적 펄스 / pulses_per_rev / 구간 길이(초) × 60, 구간 양 끝은 유효 샘플이어야 함
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import (EV_COUNTER_GAP, EV_COUNTER_RESET, ST_COUNTER_GAP, ST_COUNTER_RESET,
                     ST_ENDPOINT_INVALID, ST_INSUFFICIENT_WINDOW, ST_OK, EventRecord)

EPS = 1e-6


@dataclass
class RpmResult:
    value: Optional[float]
    status: str
    detail: Optional[str]
    event: Optional[EventRecord]


class RpmCalculator:
    def __init__(self, pulses_per_rev: float, counter_bits: int, window_s: float, max_rpm: float,
                 max_endpoint_gap_s: float) -> None:
        """max_endpoint_gap_s: 구간 시작 샘플이 (현재 - window) 보다 얼마나 더 오래될 수 있는지."""
        self.ppr = float(pulses_per_rev)
        self.modulus = 2 ** int(counter_bits)
        self.window = float(window_s)
        self.max_rpm = float(max_rpm)
        self.max_gap = float(max_endpoint_gap_s)
        self._history = []  # type: List[Tuple[float, Optional[int]]]
        self._deltas = []  # type: List[Tuple[float, int]]
        self._breaks = []  # type: List[Tuple[float, str]]  # (시각, 상태)
        self._last_valid = None  # type: Optional[Tuple[float, int]]

    def max_pulses(self, dt: float) -> float:
        return self.max_rpm / 60.0 * self.ppr * dt + 1.0  # +1: 샘플링 양자화 여유

    def update(self, t: float, count: Optional[int]) -> RpmResult:
        event = None  # type: Optional[EventRecord]
        if count is not None:
            if self._last_valid is not None:
                t_old, c_old = self._last_valid
                dt = t - t_old
                delta = (count - c_old) % self.modulus
                allowed = self.max_pulses(dt)
                if dt <= 0:
                    self._breaks.append((t, ST_COUNTER_GAP))
                elif allowed >= self.modulus:
                    # 간격이 길어 래핑 횟수를 알 수 없음
                    self._breaks.append((t, ST_COUNTER_GAP))
                    event = EventRecord(EV_COUNTER_GAP, t, {
                        "old": c_old, "new": count, "dt_s": round(dt, 6),
                        "reason": "허용 펄스 수가 카운터 범위 이상이라 증가량을 알 수 없음"})
                elif delta > allowed:
                    self._breaks.append((t, ST_COUNTER_RESET))
                    event = EventRecord(EV_COUNTER_RESET, t, {
                        "old": c_old, "new": count, "delta": delta, "dt_s": round(dt, 6),
                        "max_allowed_pulses": round(allowed, 3)})
                else:
                    self._deltas.append((t, delta))
            self._last_valid = (t, count)
        self._history.append((t, count))
        self._prune(t)
        result = self._compute(t, count)
        result.event = event
        return result

    def _prune(self, t: float) -> None:
        keep_from = t - self.window - self.max_gap - 5.0
        # 구간 시작 후보(가장 최근의 keep_from 이전 샘플 1개)는 남겨 둠
        while len(self._history) > 1 and self._history[1][0] < keep_from:
            self._history.pop(0)
        self._deltas = [d for d in self._deltas if d[0] >= keep_from]
        self._breaks = [b for b in self._breaks if b[0] >= keep_from]

    def _compute(self, t: float, count: Optional[int]) -> RpmResult:
        if count is None:
            return RpmResult(None, ST_ENDPOINT_INVALID, "현재 카운터 샘플이 무효", None)
        target = t - self.window
        start = None  # type: Optional[Tuple[float, Optional[int]]]
        for sample in self._history:
            if sample[0] <= target + EPS:
                start = sample
            else:
                break
        if start is None:
            return RpmResult(None, ST_INSUFFICIENT_WINDOW, "구간 시작 샘플 없음", None)
        t_start, c_start = start
        if c_start is None:
            return RpmResult(None, ST_ENDPOINT_INVALID, "구간 시작 샘플이 무효", None)
        if t - t_start > self.window + self.max_gap + EPS:
            return RpmResult(None, ST_INSUFFICIENT_WINDOW,
                             "구간 시작 샘플이 너무 오래됨 ({:.3f}s)".format(t - t_start), None)
        breaks = [b for b in self._breaks if t_start < b[0] <= t + EPS]
        if breaks:
            status = ST_COUNTER_RESET if any(b[1] == ST_COUNTER_RESET for b in breaks) else ST_COUNTER_GAP
            return RpmResult(None, status, "구간 안에 카운터 불연속 {}건".format(len(breaks)), None)
        total = sum(d for (tn, d) in self._deltas if t_start < tn <= t + EPS)
        length = t - t_start
        rpm = total / self.ppr / length * 60.0
        return RpmResult(rpm, ST_OK, None, None)
