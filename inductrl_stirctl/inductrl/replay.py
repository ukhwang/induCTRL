"""재생: 저장된 원시값과 배치에 저장된 설정으로 RPM·완료 후보를 다시 계산합니다.

기존 main.py 의 sensor_raw 로그는 채널 정보가 없으므로 대상이 아닙니다.
"""
import json
import sqlite3
from typing import Any, Dict, List, Optional

from . import config as config_mod
from . import db as db_mod
from .mock import apply_mock_defaults
from .models import (CH_RPM, EV_BATCH_STARTED, EV_CANDIDATE, EV_COUNTER_RESET, EV_MODBUS_RESPONSE,
                     EV_RUN_STARTED, EV_STOP_CONFIRMED, EV_STOP_OBSERVED, EV_STOP_REQUESTED,
                     EV_STOP_UNCONFIRMED, FINAL_STATUS_KO, ST_SKIPPED_BUDGET, ST_UNMAPPED, RawSample)
from .processing import BatchProcessor
from .timeutil import utc_seconds_between

ORIGINAL_EVENT_TYPES = (EV_BATCH_STARTED, EV_CANDIDATE, EV_STOP_REQUESTED, EV_MODBUS_RESPONSE,
                        EV_STOP_CONFIRMED, EV_STOP_UNCONFIRMED, EV_STOP_OBSERVED)
MATCH_TOLERANCE_S = 1e-6


class ReplayError(RuntimeError):
    pass


def _raw_samples_by_seq(rows: List[Dict[str, Any]]) -> List[List[RawSample]]:
    cycles = []  # type: List[List[RawSample]]
    current_seq = None
    for r in rows:
        if r["channel"] == CH_RPM:
            continue  # 파생값은 다시 계산
        if r["seq"] != current_seq:
            cycles.append([])
            current_seq = r["seq"]
        if r["raw_json"] is not None:
            sample = RawSample(r["channel"], r["monotonic_s"], r["collected_at_utc"],
                               [int(x) for x in json.loads(r["raw_json"])])
        else:
            status = r["status"] if r["status"] in (ST_UNMAPPED, ST_SKIPPED_BUDGET) else "READ_ERROR"
            sample = RawSample(r["channel"], r["monotonic_s"], r["collected_at_utc"], None, status,
                               r["error_detail"])
        cycles[-1].append(sample)
    return cycles


def recompute(con: sqlite3.Connection, batch_id: str,
              cfg_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    batch = db_mod.get_batch(con, batch_id)
    if batch is None:
        raise ReplayError("배치가 없습니다: " + batch_id)
    if cfg_override is None:
        cfg = json.loads(batch["config_json"])
        source = "stored"
    else:
        errors = config_mod.validate_config(cfg_override)
        if errors:
            raise config_mod.ConfigError(errors)
        cfg = apply_mock_defaults(cfg_override) if batch["mode"] == "mock" else cfg_override
        source = "override"
    events = db_mod.list_events(con, batch_id)
    started = [e for e in events if e["event_type"] == EV_RUN_STARTED]
    if not started:
        raise ReplayError("run_started 이벤트가 없어 재생할 수 없습니다")
    t_start = float(started[0]["monotonic_s"])
    rows = db_mod.list_readings(con, batch_id)
    cycles = _raw_samples_by_seq(rows)
    processor = BatchProcessor(cfg, batch["material"], batch["start_trigger"], t_start)
    candidate = None  # type: Optional[Dict[str, Any]]
    batch_started = None  # type: Optional[Dict[str, Any]]
    resets = 0
    for seq, raws in enumerate(cycles):
        res = processor.process(seq, raws)
        for ev in res.events:
            if ev.event_type == EV_COUNTER_RESET:
                resets += 1
            if ev.event_type == EV_BATCH_STARTED and batch_started is None:
                batch_started = {"monotonic_s": ev.monotonic_s, "elapsed_s": round(ev.monotonic_s - t_start, 6),
                                 "approx_utc": raws[-1].collected_at_utc}
        if res.candidate_event is not None and candidate is None:
            ev = res.candidate_event
            candidate = {"monotonic_s": ev.monotonic_s, "elapsed_s": round(ev.monotonic_s - t_start, 6),
                         "approx_utc": raws[-1].collected_at_utc, "verified": ev.detail.get("verified"),
                         "since_t0_s": ev.detail.get("since_t0_s")}
    return {"config_source": source, "config_hash": config_mod.config_hash(cfg),
            "cycles": len(cycles), "batch_started": batch_started, "candidate": candidate,
            "counter_resets": resets}


def replay_report(con: sqlite3.Connection, batch_id: str,
                  alt_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    batch = db_mod.get_batch(con, batch_id)
    if batch is None:
        raise ReplayError("배치가 없습니다: " + batch_id)
    events = db_mod.list_events(con, batch_id)
    started = [e for e in events if e["event_type"] == EV_RUN_STARTED]
    run_started_utc = started[0]["at_utc"] if started else batch["created_at_utc"]
    original = {}  # type: Dict[str, Any]
    for e in events:
        if e["event_type"] in ORIGINAL_EVENT_TYPES and e["event_type"] not in original:
            original[e["event_type"]] = {"at_utc": e["at_utc"], "elapsed_s": e["elapsed_s"],
                                         "monotonic_s": e["monotonic_s"], "detail": e["detail"]}
    stored = recompute(con, batch_id)
    report = {
        "batch_id": batch_id, "material": batch["material"], "mode": batch["mode"],
        "start_trigger": batch["start_trigger"], "final_status": batch["final_status"],
        "final_status_ko": FINAL_STATUS_KO.get(batch["final_status"], batch["final_status"]),
        "stored_config_hash": batch["config_hash"],
        "recomputed": stored, "original_events": original,
    }  # type: Dict[str, Any]
    orig_c = original.get(EV_CANDIDATE)
    rec_c = stored["candidate"]
    if orig_c is None and rec_c is None:
        report["candidate_match"] = True
    elif orig_c is None or rec_c is None:
        report["candidate_match"] = False
    else:
        report["candidate_match"] = abs(float(orig_c["monotonic_s"]) - rec_c["monotonic_s"]) <= MATCH_TOLERANCE_S
    if alt_cfg is not None:
        report["alternative"] = recompute(con, batch_id, alt_cfg)

    manual = db_mod.list_manual_entries(con, batch_id)
    stops, samples, quality = [], [], None
    for m in manual:
        when = m["measured_at_utc"] or m["entered_at_utc"]
        try:
            elapsed = round(utc_seconds_between(run_started_utc, when), 3)
        except ValueError:
            elapsed = None
        item = dict(m)
        item["elapsed_s_by_utc"] = elapsed
        if m["kind"] == "manual_stop":
            stops.append(item)
        elif m["kind"] == "sample":
            samples.append(item)
        elif m["kind"] == "quality":
            quality = item  # 마지막 입력이 최종
    report["manual_stops"] = stops
    report["samples"] = samples
    report["quality"] = quality
    return report


def _fmt_t(d: Optional[Dict[str, Any]]) -> str:
    if not d:
        return "없음"
    utc = d.get("at_utc") or d.get("approx_utc") or ""
    return "{:+.3f}s  {}".format(float(d["elapsed_s"]), utc)


def format_report(rep: Dict[str, Any]) -> str:
    lines = []  # type: List[str]
    lines.append("=== 배치 {} ({} / {} / 시작 트리거 {}) ===".format(
        rep["batch_id"], rep["material"], rep["mode"], rep["start_trigger"]))
    lines.append("최종 상태: {} ({})".format(rep["final_status"], rep["final_status_ko"]))
    rc = rep["recomputed"]
    lines.append("[재계산 · 저장된 설정 {}]".format(rep["stored_config_hash"][:12]))
    lines.append("  batch_started : {}".format(_fmt_t(rc["batch_started"])))
    cand = rc["candidate"]
    lines.append("  candidate     : {}{}".format(
        _fmt_t(cand), "" if not cand else ("  (검증)" if cand.get("verified") else "  (미검증 판정)")))
    lines.append("  카운터 리셋   : {}건, 주기 {}개".format(rc["counter_resets"], rc["cycles"]))
    lines.append("  원래 기록과 후보 시각 일치: {}".format("예" if rep["candidate_match"] else "아니오"))
    if "alternative" in rep:
        alt = rep["alternative"]
        lines.append("[재계산 · 다른 설정 {}]".format(alt["config_hash"][:12]))
        lines.append("  batch_started : {}".format(_fmt_t(alt["batch_started"])))
        lines.append("  candidate     : {}".format(_fmt_t(alt["candidate"])))
        if cand and alt["candidate"]:
            lines.append("  차이(다른 설정 - 저장 설정): {:+.3f}s".format(
                alt["candidate"]["elapsed_s"] - cand["elapsed_s"]))
    lines.append("[원래 기록된 이벤트]")
    for key in ORIGINAL_EVENT_TYPES:
        ev = rep["original_events"].get(key)
        extra = ""
        if ev and key == EV_MODBUS_RESPONSE:
            extra = "  outcome={}".format(ev["detail"].get("outcome"))
        if ev and key in (EV_STOP_UNCONFIRMED, EV_STOP_CONFIRMED, EV_STOP_OBSERVED):
            extra = "  {}".format(ev["detail"].get("reason") or ev["detail"].get("method") or "")
        lines.append("  {:<17}: {}{}".format(key, _fmt_t(ev), extra))
    lines.append("[수동 정지]")
    if not rep["manual_stops"]:
        lines.append("  없음")
    for m in rep["manual_stops"]:
        lines.append("  {} (시작 기준 {}s) {}".format(m["measured_at_utc"] or m["entered_at_utc"],
                                                   m["elapsed_s_by_utc"], m["note"] or ""))
    lines.append("[시료 측정값]")
    if not rep["samples"]:
        lines.append("  없음")
    for m in rep["samples"]:
        lines.append("  {:<6} {} = {} {} (측정 {}, 시작 기준 {}s)".format(
            m["position"] or "-", m["metric"], m["value"], m["unit"] or "",
            m["measured_at_utc"] or m["entered_at_utc"], m["elapsed_s_by_utc"]))
    q = rep["quality"]
    lines.append("[품질 합격 여부] {}".format(
        "미입력" if q is None else ("합격" if q["passed"] == 1 else "불합격") + (
            "  ({})".format(q["note"]) if q.get("note") else "")))
    return "\n".join(lines)
