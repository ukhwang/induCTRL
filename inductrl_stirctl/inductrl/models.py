"""공용 데이터 구조와 상태 코드."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- 채널 이름 -------------------------------------------------------------
CH_TURBIDITY_TOP = "turbidity_top"
CH_TURBIDITY_BOTTOM = "turbidity_bottom"
CH_CONDUCTIVITY = "conductivity"
CH_ROTATION = "rotation_counter"
CH_RPM = "rpm"                    # 파생 채널(저장용). replay는 이 행을 쓰지 않고 다시 계산합니다.
CH_STOP_FEEDBACK = "stop_feedback"

ANALOG_CHANNELS = (CH_TURBIDITY_TOP, CH_TURBIDITY_BOTTOM, CH_CONDUCTIVITY)
ALL_CHANNELS = ANALOG_CHANNELS + (CH_ROTATION,)
DERIVED_CHANNELS = (CH_RPM,)

MATERIALS = ("saltwater", "caco3")
MATERIAL_CHANNELS = {
    "saltwater": (CH_CONDUCTIVITY,),
    "caco3": (CH_TURBIDITY_TOP, CH_TURBIDITY_BOTTOM),
}
MODES = ("mock", "observe", "live")
START_TRIGGERS = ("rpm", "launch")

# --- 수신(읽기) 단계 상태: 원시값이 없을 때 ----------------------------------
ST_OK = "OK"
ST_READ_ERROR = "READ_ERROR"
ST_SKIPPED_BUDGET = "SKIPPED_BUDGET"
ST_UNMAPPED = "UNMAPPED"
READ_LEVEL_STATUSES = (ST_READ_ERROR, ST_SKIPPED_BUDGET, ST_UNMAPPED)

# --- 처리 단계 상태 ---------------------------------------------------------
ST_DECODE_ERROR = "DECODE_ERROR"
ST_OUT_OF_RANGE = "OUT_OF_RANGE"
ST_STALE = "STALE"

# --- RPM 파생 상태 ----------------------------------------------------------
ST_COUNTER_RESET = "COUNTER_RESET"
ST_COUNTER_GAP = "COUNTER_GAP"
ST_INSUFFICIENT_WINDOW = "INSUFFICIENT_WINDOW"
ST_ENDPOINT_INVALID = "ENDPOINT_INVALID"
ST_RPM_UNCONFIGURED = "RPM_UNCONFIGURED"

# --- 이벤트 종류 -------------------------------------------------------------
EV_RUN_STARTED = "run_started"
EV_BATCH_STARTED = "batch_started"
EV_WARNING = "warning"
EV_OVERRUN = "overrun"
EV_COUNTER_RESET = "COUNTER_RESET"
EV_COUNTER_GAP = "counter_gap"
EV_HOLD_RESET = "hold_reset"
EV_CANDIDATE = "candidate"
EV_STOP_BLOCKED = "stop_blocked"
EV_STOP_REQUESTED = "stop_requested"
EV_MODBUS_RESPONSE = "modbus_response"
EV_STOP_CONFIRMED = "stop_confirmed"
EV_STOP_UNCONFIRMED = "stop_unconfirmed"
EV_STOP_OBSERVED = "stop_observed"
EV_RUN_FINISHED = "run_finished"
EV_ERROR = "error"

# --- 배치 최종 상태 ----------------------------------------------------------
FS_RUNNING = "RUNNING"
FS_AUTO_STOP_CONFIRMED = "AUTO_STOP_CONFIRMED"
FS_STOP_UNCONFIRMED = "STOP_UNCONFIRMED"
FS_CANDIDATE_RECORDED = "CANDIDATE_RECORDED"
FS_CANDIDATE_UNVERIFIED = "CANDIDATE_UNVERIFIED"
FS_NO_CANDIDATE = "NO_CANDIDATE"
FS_INTERRUPTED = "INTERRUPTED"
FS_ABORTED = "ABORTED_ERROR"

FINAL_STATUS_KO = {
    FS_RUNNING: "실행 중(또는 비정상 종료)",
    FS_AUTO_STOP_CONFIRMED: "자동 정지 확인",
    FS_STOP_UNCONFIRMED: "정지 미확인 — 운영자 확인 필요",
    FS_CANDIDATE_RECORDED: "완료 후보 기록(정지 명령 없음)",
    FS_CANDIDATE_UNVERIFIED: "미검증 판정으로 완료 후보 기록(정지 명령 없음)",
    FS_NO_CANDIDATE: "완료 후보 없음",
    FS_INTERRUPTED: "사용자 중단(정지 명령 없음)",
    FS_ABORTED: "오류로 중단(정지 명령 없음)",
}


@dataclass
class RawSample:
    """한 주기에서 한 채널을 읽은 결과(원시)."""

    channel: str
    monotonic_s: float
    collected_at_utc: str
    registers: Optional[List[int]]
    read_status: Optional[str] = None   # None이면 registers 수신. 아니면 READ_LEVEL_STATUSES 중 하나
    error_detail: Optional[str] = None


@dataclass
class Reading:
    """저장할 처리 결과 1행."""

    channel: str
    monotonic_s: float
    collected_at_utc: str
    raw_json: Optional[str]
    value: Optional[float]
    unit: Optional[str]
    status: str
    error_detail: Optional[str] = None


@dataclass
class EventRecord:
    event_type: str
    monotonic_s: float
    detail: Dict[str, Any] = field(default_factory=dict)
