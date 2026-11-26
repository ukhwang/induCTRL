"""정지 확인 상태기계. 이 파일에는 Modbus 쓰기가 없습니다(판정만 함).

- 쓰기 성공 후 stop_confirm_timeout_s 안에
    (a) 유효 RPM <= stopped_rpm_max 가 stop_hold_seconds 동안 유지, 또는
    (b) 검증된 정지 피드백이 stopped_value 를 보고
  하면 stop_confirmed.
- 쓰기 결과가 불명확(예외/타임아웃/연결 오류)하면 즉시 stop_unconfirmed.
  이후 RPM 이 멈춘 것으로 보이면 stop_observed 만 기록하고 '자동 정지 확인'으로 바꾸지 않습니다.
"""
from typing import Any, List, Optional

from .models import EV_STOP_CONFIRMED, EV_STOP_OBSERVED, EV_STOP_UNCONFIRMED, EventRecord

EPS = 1e-6

IDLE = "IDLE"
CONFIRMING = "CONFIRMING"
CONFIRMED = "CONFIRMED"
UNCONFIRMED = "UNCONFIRMED"


class StopMonitor:
    def __init__(self, stopped_rpm_max: float, stop_hold_s: float, confirm_timeout_s: float,
                 feedback_enabled: bool, feedback_stopped_value: Optional[Any]) -> None:
        self.stopped_rpm_max = float(stopped_rpm_max)
        self.hold = float(stop_hold_s)
        self.timeout = float(confirm_timeout_s)
        self.feedback_enabled = feedback_enabled
        self.feedback_stopped = (None if feedback_stopped_value is None
                                 else float(int(feedback_stopped_value)))
        self.state = IDLE
        self.requested_t = None  # type: Optional[float]
        self.deadline = None  # type: Optional[float]
        self._low_since = None  # type: Optional[float]
        self.observed = False

    def on_write_result(self, t_request: float, t_response: float, outcome: str,
                        detail: str) -> List[EventRecord]:
        self.requested_t = t_request
        if outcome == "success":
            self.state = CONFIRMING
            self.deadline = t_request + self.timeout
            return []
        self.state = UNCONFIRMED
        return [EventRecord(EV_STOP_UNCONFIRMED, t_response, {
            "reason": "write_" + outcome,
            "detail": detail,
            "message": "정지 쓰기 결과 불명확 — 응답 없음은 성공이 아닙니다. 운영자가 확인·수동 정지하세요.",
        })]

    def _stopped_now(self, t: float, rpm: Optional[float],
                     feedback: Optional[float]) -> Optional[str]:
        if self.feedback_enabled and feedback is not None and self.feedback_stopped is not None:
            if abs(feedback - self.feedback_stopped) < EPS:
                return "feedback"
        if rpm is not None and rpm <= self.stopped_rpm_max + EPS:
            if self._low_since is None:
                self._low_since = t
            if t - self._low_since + EPS >= self.hold:
                return "rpm"
        else:
            self._low_since = None
        return None

    def update(self, t: float, rpm: Optional[float], feedback: Optional[float]) -> List[EventRecord]:
        if self.state == CONFIRMING:
            assert self.deadline is not None
            method = self._stopped_now(t, rpm, feedback)
            if method is not None and t <= self.deadline + EPS:
                self.state = CONFIRMED
                return [EventRecord(EV_STOP_CONFIRMED, t, {
                    "method": method, "rpm": rpm, "feedback": feedback,
                    "since_request_s": round(t - (self.requested_t or t), 6)})]
            if t > self.deadline + EPS:
                self.state = UNCONFIRMED
                self._low_since = None
                return [EventRecord(EV_STOP_UNCONFIRMED, t, {
                    "reason": "confirm_timeout",
                    "detail": "stop_confirm_timeout_s={} 안에 정지가 확인되지 않음 (rpm={})".format(
                        self.timeout, rpm),
                    "message": "정지가 확인되지 않았습니다. 운영자가 확인·수동 정지하세요.",
                })]
            return []
        if self.state == UNCONFIRMED and not self.observed:
            method = self._stopped_now(t, rpm, feedback)
            if method is not None:
                self.observed = True
                return [EventRecord(EV_STOP_OBSERVED, t, {
                    "method": method, "rpm": rpm, "feedback": feedback,
                    "note": "정지된 것으로 보이나 자동 정지 확인으로 처리하지 않음"})]
        return []
