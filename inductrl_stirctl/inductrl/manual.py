"""수동 입력(시료·품질·수동 정지)과 배치 요약."""
import sqlite3
import time
from typing import Any, Dict, Optional

from . import db as db_mod
from .models import FINAL_STATUS_KO
from .timeutil import parse_iso_to_utc, utc_now_iso

POSITIONS = ("top", "mid", "bottom")


class ManualEntryError(RuntimeError):
    pass


def _require_batch(con: sqlite3.Connection, batch_id: str) -> None:
    if not db_mod.batch_exists(con, batch_id):
        raise ManualEntryError("배치가 없습니다: {} (먼저 run 으로 배치를 만드세요)".format(batch_id))


def add_sample(con: sqlite3.Connection, batch_id: str, position: str, metric: str, value: float,
               unit: Optional[str], measured_at: Optional[str], note: Optional[str] = None) -> int:
    _require_batch(con, batch_id)
    if position not in POSITIONS:
        raise ManualEntryError("position 은 top/mid/bottom 중 하나")
    measured = parse_iso_to_utc(measured_at) if measured_at else None
    return db_mod.insert_manual_entry(con, batch_id, "sample", utc_now_iso(), position=position,
                                      metric=metric, value=float(value), unit=unit,
                                      measured_at_utc=measured, note=note)


def set_quality(con: sqlite3.Connection, batch_id: str, passed: bool, note: Optional[str]) -> int:
    _require_batch(con, batch_id)
    return db_mod.insert_manual_entry(con, batch_id, "quality", utc_now_iso(), metric="quality",
                                      passed=passed, note=note)


def manual_stop(con: sqlite3.Connection, batch_id: str, at: Optional[str], note: Optional[str]) -> int:
    """운영자 수동 정지 시각 기록. --at 이 없으면 지금 시각과 monotonic 시각을 함께 남깁니다."""
    _require_batch(con, batch_id)
    now = utc_now_iso()
    if at:
        return db_mod.insert_manual_entry(con, batch_id, "manual_stop", now,
                                          measured_at_utc=parse_iso_to_utc(at), note=note)
    return db_mod.insert_manual_entry(con, batch_id, "manual_stop", now, measured_at_utc=now,
                                      monotonic_s=time.monotonic(), note=note)


def batch_summary(con: sqlite3.Connection, batch_id: str) -> Dict[str, Any]:
    batch = db_mod.get_batch(con, batch_id)
    if batch is None:
        raise ManualEntryError("배치가 없습니다: " + batch_id)
    batch.pop("config_json", None)
    return {
        "batch": batch,
        "final_status_ko": FINAL_STATUS_KO.get(batch["final_status"], batch["final_status"]),
        "events": [e for e in db_mod.list_events(con, batch_id) if e["event_type"] != "hold_reset"],
        "hold_resets": sum(1 for e in db_mod.list_events(con, batch_id) if e["event_type"] == "hold_reset"),
        "manual_entries": db_mod.list_manual_entries(con, batch_id),
        "reading_counts": db_mod.reading_status_counts(con, batch_id),
    }


def format_summary(s: Dict[str, Any]) -> str:
    b = s["batch"]
    lines = [
        "=== 배치 {} ===".format(b["batch_id"]),
        "재료 {} / 모드 {} / 시작 트리거 {} / 시나리오 {}".format(
            b["material"], b["mode"], b["start_trigger"], b["scenario"] or "-"),
        "생성 {} / 종료 {}".format(b["created_at_utc"], b["finished_at_utc"] or "-"),
        "설정 해시 {}".format(b["config_hash"]),
        "최종 상태: {} ({})".format(b["final_status"], s["final_status_ko"]),
        "",
        "[이벤트] (hold_reset {}건 생략)".format(s["hold_resets"]),
    ]
    for e in s["events"]:
        elapsed = "-" if e["elapsed_s"] is None else "{:+.3f}s".format(e["elapsed_s"])
        lines.append("  {:>10}  {:<17} {}  {}".format(elapsed, e["event_type"], e["at_utc"],
                                                     _compact(e["detail"])))
    lines.append("")
    lines.append("[수동 입력]")
    if not s["manual_entries"]:
        lines.append("  없음")
    for m in s["manual_entries"]:
        if m["kind"] == "sample":
            lines.append("  sample   {:<6} {} = {} {}  측정 {}  {}".format(
                m["position"], m["metric"], m["value"], m["unit"] or "", m["measured_at_utc"] or "-",
                m["note"] or ""))
        elif m["kind"] == "quality":
            lines.append("  quality  {}  입력 {}  {}".format(
                "합격" if m["passed"] == 1 else "불합격", m["entered_at_utc"], m["note"] or ""))
        else:
            lines.append("  manual_stop  {}  {}".format(m["measured_at_utc"], m["note"] or ""))
    lines.append("")
    lines.append("[수집 상태 집계]")
    for r in s["reading_counts"]:
        lines.append("  {:<18} {:<20} {}".format(r["channel"], r["status"], r["n"]))
    return "\n".join(lines)


def _compact(detail: Dict[str, Any]) -> str:
    skip = ("window_stats", "unverified_reasons")
    parts = ["{}={}".format(k, v) for k, v in detail.items() if k not in skip]
    text = " ".join(parts)
    return text if len(text) <= 160 else text[:157] + "..."
