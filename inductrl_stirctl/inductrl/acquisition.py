"""한 주기 수집: 채널별 개별 요청 + 주기 시간 예산."""
import abc
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import (EV_OVERRUN, ST_READ_ERROR, ST_SKIPPED_BUDGET, ST_UNMAPPED, EventRecord,
                     RawSample)


class SampleSource(abc.ABC):
    """채널 원시값을 읽는 쪽(가상 센서 또는 Modbus). 쓰기 기능은 없습니다."""

    @abc.abstractmethod
    def read_channel(self, name: str, ch_cfg: Dict[str, Any]) -> List[int]:
        """레지스터 목록을 돌려주거나, 실패하면 예외를 던집니다."""

    @abc.abstractmethod
    def read_feedback(self, fb_cfg: Dict[str, Any]) -> List[int]:
        """정지 피드백 값 1개를 [정수] 로 돌려줍니다(코일/DI 는 0/1)."""

    def close(self) -> None:
        return None


@dataclass
class PlanItem:
    channel: str
    cfg: Dict[str, Any]
    mapped: bool
    is_feedback: bool = False
    unmapped_reason: Optional[str] = None


def acquire_cycle(source: SampleSource, plan: List[PlanItem], clock: Callable[[], float],
                  utc_now: Callable[[], str], cycle_start: float,
                  budget_s: float) -> Tuple[List[RawSample], Optional[EventRecord]]:
    samples = []  # type: List[RawSample]
    skipped = []  # type: List[str]
    for item in plan:
        if not item.mapped:
            samples.append(RawSample(item.channel, clock(), utc_now(), None, ST_UNMAPPED,
                                     item.unmapped_reason))
            continue
        spent = clock() - cycle_start
        if spent >= budget_s:
            skipped.append(item.channel)
            samples.append(RawSample(item.channel, clock(), utc_now(), None, ST_SKIPPED_BUDGET,
                                     "주기 예산 {:.3f}s 중 {:.3f}s 사용".format(budget_s, spent)))
            continue
        try:
            if item.is_feedback:
                regs = source.read_feedback(item.cfg)
            else:
                regs = source.read_channel(item.channel, item.cfg)
            samples.append(RawSample(item.channel, clock(), utc_now(), [int(r) for r in regs]))
        except Exception as exc:  # 통신 오류는 상태로 남기고 계속 진행
            samples.append(RawSample(item.channel, clock(), utc_now(), None, ST_READ_ERROR,
                                     "{}: {}".format(type(exc).__name__, exc)))
    total = clock() - cycle_start
    overrun = None
    if skipped or total > budget_s:
        overrun = EventRecord(EV_OVERRUN, clock(), {
            "read_seconds": round(total, 6), "budget_s": budget_s, "skipped": skipped})
    return samples, overrun
