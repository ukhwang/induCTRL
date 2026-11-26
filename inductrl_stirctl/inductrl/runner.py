"""배치 실행 루프 (mock / observe / live)."""
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TextIO

from . import actuator as actuator_mod
from . import config as config_mod
from . import db as db_mod
from . import modbus_io
from .acquisition import PlanItem, SampleSource, acquire_cycle
from .actuator import MockActuator, NoWriteActuator, StopActuator, StopWriteResult
from .mock import SCENARIOS, MockPlant, MockSource, apply_mock_defaults
from .models import (ANALOG_CHANNELS, CH_ROTATION, CH_STOP_FEEDBACK, EV_ERROR,
                     EV_MODBUS_RESPONSE, EV_RUN_FINISHED, EV_RUN_STARTED, EV_STOP_BLOCKED,
                     EV_STOP_REQUESTED, EV_STOP_UNCONFIRMED,
                     EV_WARNING, FS_ABORTED, FS_AUTO_STOP_CONFIRMED, FS_CANDIDATE_RECORDED,
                     FS_CANDIDATE_UNVERIFIED, FS_INTERRUPTED, FS_NO_CANDIDATE, FS_RUNNING,
                     FS_STOP_UNCONFIRMED, MATERIAL_CHANNELS, EventRecord)
from .processing import BatchProcessor, CycleResult
from .stopctl import CONFIRMED, StopMonitor
from .timeutil import SimClock, real_clock, real_sleep, utc_now_iso

MOCK_DEFAULT_SAMPLES = 240


class LiveGateError(RuntimeError):
    def __init__(self, missing: List[str]) -> None:
        super().__init__("live 모드 시작 거부 — 빠진 조건:\n  - " + "\n  - ".join(missing))
        self.missing = missing


class BatchExistsError(RuntimeError):
    pass


@dataclass
class RunOptions:
    mode: str
    material: str
    batch_id: str
    start_trigger: str = "rpm"
    arm_stop: bool = False
    samples: int = 0
    scenario: str = "normal"
    fast: bool = False
    db_path: Optional[str] = None
    status_every: int = 10


@dataclass
class RunSummary:
    batch_id: str
    final_status: str
    cycles: int
    candidate_t: Optional[float]
    stop_requested: bool
    events: List[str] = field(default_factory=list)
    mock_actuator: Optional[MockActuator] = None


def build_plan(cfg: Dict[str, Any], material: str, mode: str) -> List[PlanItem]:
    """읽기 순서: 회전 카운터 → 판정 채널 → 나머지 → 정지 피드백."""
    order = [CH_ROTATION] + list(MATERIAL_CHANNELS[material])
    order += [c for c in ANALOG_CHANNELS if c not in order]
    plan = []  # type: List[PlanItem]
    for name in order:
        ch = cfg["channels"][name]
        if mode == "mock":
            plan.append(PlanItem(name, ch, True))
            continue
        mapped = config_mod.channel_is_mapped(name, ch)
        reason = None
        if not mapped:
            missing = config_mod.missing_mapping_fields(name, ch)
            reason = "mapping_verified={} / null: {}".format(ch.get("mapping_verified"),
                                                             ", ".join(missing) or "-")
        plan.append(PlanItem(name, ch, mapped, False, reason))
    fb = cfg.get("stop_feedback")
    if mode != "mock" and isinstance(fb, dict) and fb.get("verified") is True and fb.get("address") is not None:
        plan.append(PlanItem(CH_STOP_FEEDBACK, fb, True, True))
    return plan


def _status_line(res: CycleResult, t_start: float) -> str:
    rpm = "-" if res.rpm is None else "{:.1f}".format(res.rpm)
    hold = "-" if res.decision.hold_s is None else "{:.1f}s".format(res.decision.hold_s)
    reasons = ",".join(res.decision.reasons[:4])
    return "[{:7.1f}s] seq={} rpm={} state={} hold={} {}".format(
        res.t_eval - t_start, res.seq, rpm, res.decision.state, hold, reasons)


class BatchRunner:
    def __init__(self, cfg: Dict[str, Any], opts: RunOptions,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 out: Optional[TextIO] = None, err: Optional[TextIO] = None) -> None:
        self.opts = opts
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        if opts.mode not in ("mock", "observe", "live"):
            raise ValueError("mode 는 mock/observe/live 중 하나")
        if opts.mode != "mock" and (opts.fast or opts.scenario != "normal"):
            raise ValueError("--fast / --scenario 는 mock 모드 전용입니다")

        errors = config_mod.validate_config(cfg)
        if errors:
            raise config_mod.ConfigError(errors)
        if opts.mode == "live":
            missing = config_mod.check_live_readiness(cfg, opts.material, opts.arm_stop)
            if missing:
                raise LiveGateError(missing)

        self.cfg = apply_mock_defaults(cfg) if opts.mode == "mock" else config_mod.deep_copy(cfg)
        if opts.mode == "mock":
            errors = config_mod.validate_config(self.cfg)
            if errors:
                raise config_mod.ConfigError(errors)
            if opts.scenario not in SCENARIOS:
                raise ValueError("알 수 없는 시나리오: {} (가능: {})".format(
                    opts.scenario, ", ".join(sorted(SCENARIOS))))

        if clock is None:
            if opts.mode == "mock" and opts.fast:
                sim = SimClock()
                clock, sleep = sim.now, sim.sleep
            else:
                clock, sleep = real_clock, real_sleep
        self.clock = clock
        self.sleep = sleep if sleep is not None else real_sleep
        self.interval = float(self.cfg["decision"]["interval_seconds"])
        self.budget = float(self.cfg["cycle_budget_s"])
        self.samples = opts.samples
        if opts.mode == "mock" and self.samples == 0 and opts.fast:
            self.samples = MOCK_DEFAULT_SAMPLES
        self.db_path = opts.db_path or self.cfg["database"]

        self.source = None  # type: Optional[SampleSource]
        self.actuator = None  # type: Optional[StopActuator]
        self.plant = None  # type: Optional[MockPlant]
        self.stop_monitor = None  # type: Optional[StopMonitor]
        self.stop_requested = False
        self.candidate_verified = None  # type: Optional[bool]
        self.event_log = []  # type: List[str]

    # ------------------------------------------------------------------ #
    def _build_io(self) -> None:
        cfg, opts = self.cfg, self.opts
        if opts.mode == "mock":
            # Modbus 클라이언트를 만들지 않습니다.
            self.plant = MockPlant(cfg, opts.material, SCENARIOS[opts.scenario], self.clock)
            self.source = MockSource(self.plant, self.clock, self.sleep)
            sc = SCENARIOS[opts.scenario]
            self.actuator = MockActuator(self.clock, sc.actuator_outcome, self.plant.stop, sc.motor_stops)
            return
        raw_client = modbus_io.create_tcp_client(cfg["host"], int(cfg["port"]), float(cfg["read_timeout_s"]))
        read_client = modbus_io.ReadOnlyModbusClient(raw_client)
        self.source = modbus_io.ModbusSource(read_client, int(cfg["unit_id"]))
        if opts.mode == "observe":
            self.actuator = NoWriteActuator()
        else:
            self.actuator = actuator_mod.ModbusStopActuator(cfg, self.clock)

    def _make_stop_monitor(self) -> Optional[StopMonitor]:
        act = self.cfg["actuator"]
        if any(act.get(k) is None for k in ("stopped_rpm_max", "stop_hold_seconds", "stop_confirm_timeout_s")):
            return None
        fb = self.cfg.get("stop_feedback")
        fb_on = (self.opts.mode != "mock" and isinstance(fb, dict) and fb.get("verified") is True
                 and fb.get("address") is not None)
        return StopMonitor(act["stopped_rpm_max"], act["stop_hold_seconds"], act["stop_confirm_timeout_s"],
                           fb_on, fb.get("stopped_value") if fb_on else None)

    def _event(self, con: Any, ev: EventRecord, t_start: float) -> None:
        db_mod.insert_event(con, self.opts.batch_id, ev, utc_now_iso(), t_start)
        self.event_log.append(ev.event_type)
        if ev.event_type not in ("hold_reset", "overrun"):
            print("  [event] {} @ {:.3f}s {}".format(ev.event_type, ev.monotonic_s - t_start,
                                                     _short(ev.detail)), file=self.out, flush=True)
        if ev.event_type in (EV_STOP_UNCONFIRMED,):
            print("경고: 정지 미확인 — {}. 운영자가 즉시 확인·수동 정지하세요.".format(
                ev.detail.get("reason")), file=self.err, flush=True)

    # ------------------------------------------------------------------ #
    def run(self) -> RunSummary:
        opts = self.opts
        con = db_mod.connect(self.db_path)
        if db_mod.batch_exists(con, opts.batch_id):
            con.close()
            raise BatchExistsError("batch_id '{}' 가 이미 있습니다. 새 batch_id 를 쓰세요.".format(opts.batch_id))
        cfg_json = config_mod.canonical_json(self.cfg)
        db_mod.create_batch(con, opts.batch_id, opts.material, opts.mode, opts.start_trigger,
                            opts.scenario if opts.mode == "mock" else None, utc_now_iso(),
                            cfg_json, config_mod.config_hash(self.cfg), FS_RUNNING)
        final_status = FS_RUNNING
        seq = 0
        processor = None  # type: Optional[BatchProcessor]
        t_start = self.clock()
        try:
            self._build_io()
            self.stop_monitor = self._make_stop_monitor()
            plan = build_plan(self.cfg, opts.material, opts.mode)
            t_start = self.clock()
            self._event(con, EventRecord(EV_RUN_STARTED, t_start, {
                "mode": opts.mode, "material": opts.material, "start_trigger": opts.start_trigger,
                "arm_stop": opts.arm_stop, "scenario": opts.scenario if opts.mode == "mock" else None,
                "fast": opts.fast, "unverified": config_mod.unverified_reasons(self.cfg, opts.material),
            }), t_start)
            for item in plan:
                if not item.mapped and item.channel in config_mod.required_channels(opts.material):
                    self._event(con, EventRecord(EV_WARNING, t_start, {
                        "channel": item.channel, "message": "판정에 필요한 채널이 UNMAPPED — 판정하지 않음",
                        "reason": item.unmapped_reason}), t_start)
            con.commit()
            processor = BatchProcessor(self.cfg, opts.material, opts.start_trigger, t_start)
            assert self.source is not None
            while self.samples == 0 or seq < self.samples:
                cycle_start = self.clock()
                raws, overrun = acquire_cycle(self.source, plan, self.clock, utc_now_iso,
                                              cycle_start, self.budget)
                res = processor.process(seq, raws)
                db_mod.insert_readings(con, opts.batch_id, seq, res.readings, t_start)
                d = res.decision
                db_mod.insert_decision(con, opts.batch_id, seq, res.t_eval, t_start, d.state, res.rpm,
                                       d.hold_s, d.verified, d.reasons)
                if overrun is not None:
                    self._event(con, overrun, t_start)
                for ev in res.events:
                    self._event(con, ev, t_start)
                if res.candidate_event is not None:
                    self.candidate_verified = bool(res.candidate_event.detail.get("verified"))
                    con.commit()
                    self._handle_candidate(con, res, t_start)
                if self.stop_monitor is not None and self.stop_requested:
                    for ev in self.stop_monitor.update(res.t_eval, res.rpm, res.feedback_value):
                        self._event(con, ev, t_start)
                con.commit()
                if opts.status_every and seq % opts.status_every == 0:
                    print(_status_line(res, t_start), file=self.out, flush=True)
                seq += 1
                if self.samples and seq >= self.samples:
                    break
                remaining = self.interval - (self.clock() - cycle_start)
                if remaining > 0:
                    self.sleep(remaining)
            final_status = self._final_status(interrupted=False)
        except KeyboardInterrupt:
            final_status = self._final_status(interrupted=True)
            raise
        except Exception as exc:
            final_status = FS_ABORTED
            try:
                self._event(con, EventRecord(EV_ERROR, self.clock(), {
                    "error": "{}: {}".format(type(exc).__name__, exc)}), t_start)
            except Exception:
                pass
            raise
        finally:
            # 종료 처리: 소켓만 닫습니다. DO 해제 쓰기는 하지 않습니다.
            try:
                self._event(con, EventRecord(EV_RUN_FINISHED, self.clock(), {
                    "final_status": final_status, "cycles": seq}), t_start)
                db_mod.finish_batch(con, opts.batch_id, final_status, utc_now_iso())
            finally:
                if self.source is not None:
                    self.source.close()
                if self.actuator is not None:
                    self.actuator.close()
                con.close()
        cand_t = processor.decision.candidate_t if processor is not None else None
        return RunSummary(opts.batch_id, final_status, seq, cand_t, self.stop_requested,
                          list(self.event_log),
                          self.actuator if isinstance(self.actuator, MockActuator) else None)

    # ------------------------------------------------------------------ #
    def _handle_candidate(self, con: Any, res: CycleResult, t_start: float) -> None:
        opts = self.opts
        if self.stop_requested:
            return
        verified = bool(res.candidate_event.detail.get("verified")) if res.candidate_event else False
        if opts.mode == "observe":
            print("  observe 모드: 후보 시각만 기록 (쓰기 0회)", file=self.out, flush=True)
            return
        if opts.mode == "live" and not (verified and opts.arm_stop):
            self._event(con, EventRecord(EV_STOP_BLOCKED, res.t_eval, {
                "reason": "미검증 판정 또는 --arm-stop 없음 — live 정지 안 함"}), t_start)
            return
        if self.stop_monitor is None:
            self._event(con, EventRecord(EV_STOP_BLOCKED, res.t_eval, {
                "reason": "actuator.stopped_rpm_max/stop_hold_seconds/stop_confirm_timeout_s 미설정"}), t_start)
            return
        assert self.actuator is not None
        self.stop_requested = True
        t_req = self.clock()
        self._event(con, EventRecord(EV_STOP_REQUESTED, t_req, {
            "mode": opts.mode, "actuator": self.actuator.kind,
            "address": self.cfg["actuator"].get("stop_coil_address"),
            "value": self.cfg["actuator"].get("stop_value"),
            "unit_id": self.cfg.get("unit_id")}), t_start)
        con.commit()  # 쓰기 전에 요청 사실을 먼저 저장
        try:
            result = self.actuator.request_stop()
        except Exception as exc:  # 방어: 액추에이터 내부 예외도 '불명확'으로 처리
            result = StopWriteResult("error", "{}: {}".format(type(exc).__name__, exc), utc_now_iso(),
                                     self.clock(), 0)
        self._event(con, EventRecord(EV_MODBUS_RESPONSE, result.monotonic_s, result.as_detail()), t_start)
        for ev in self.stop_monitor.on_write_result(t_req, result.monotonic_s, result.outcome, result.detail):
            self._event(con, ev, t_start)
        con.commit()

    def _final_status(self, interrupted: bool) -> str:
        if self.stop_requested:
            if self.stop_monitor is not None and self.stop_monitor.state == CONFIRMED:
                return FS_AUTO_STOP_CONFIRMED
            return FS_STOP_UNCONFIRMED
        if self.candidate_verified is not None:
            return FS_CANDIDATE_RECORDED if self.candidate_verified else FS_CANDIDATE_UNVERIFIED
        return FS_INTERRUPTED if interrupted else FS_NO_CANDIDATE


def _short(detail: Dict[str, Any]) -> str:
    keys = ("trigger", "verified", "label", "since_t0_s", "outcome", "reason", "method", "old", "new",
            "skipped", "message", "final_status", "mode")
    parts = ["{}={}".format(k, detail[k]) for k in keys if k in detail]
    return " ".join(parts)


def run_batch(cfg: Dict[str, Any], opts: RunOptions, clock: Optional[Callable[[], float]] = None,
              sleep: Optional[Callable[[float], None]] = None, out: Optional[TextIO] = None,
              err: Optional[TextIO] = None) -> RunSummary:
    return BatchRunner(cfg, opts, clock, sleep, out, err).run()
