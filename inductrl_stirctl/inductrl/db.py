"""SQLite 저장소 (WAL + busy_timeout).

로거(run)와 CLI 수동 입력이 같은 DB 파일에 동시에 씁니다.
기존 main.py 의 sensor_raw 테이블은 건드리지 않습니다.
"""
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .models import EventRecord, Reading

BUSY_TIMEOUT_MS = 5000

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS batches (
        batch_id TEXT PRIMARY KEY,
        material TEXT NOT NULL,
        mode TEXT NOT NULL,
        start_trigger TEXT NOT NULL,
        scenario TEXT,
        created_at_utc TEXT NOT NULL,
        finished_at_utc TEXT,
        config_json TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        final_status TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        channel TEXT NOT NULL,
        collected_at_utc TEXT NOT NULL,
        monotonic_s REAL NOT NULL,
        elapsed_s REAL NOT NULL,
        raw_json TEXT,
        value REAL,
        unit TEXT,
        status TEXT NOT NULL,
        error_detail TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_readings_batch_seq ON readings(batch_id, seq)",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        at_utc TEXT NOT NULL,
        monotonic_s REAL,
        elapsed_s REAL,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id, id)",
    """CREATE TABLE IF NOT EXISTS decision_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        monotonic_s REAL NOT NULL,
        elapsed_s REAL NOT NULL,
        state TEXT NOT NULL,
        rpm REAL,
        hold_s REAL,
        verified INTEGER,
        reasons_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_decision_batch_seq ON decision_log(batch_id, seq)",
    """CREATE TABLE IF NOT EXISTS manual_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        position TEXT,
        metric TEXT,
        value REAL,
        unit TEXT,
        passed INTEGER,
        entered_at_utc TEXT NOT NULL,
        measured_at_utc TEXT,
        monotonic_s REAL,
        note TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_manual_batch ON manual_entries(batch_id, id)",
    # --- 실시간 시뮬레이터(선택) : 기존 테이블은 그대로 두고 옆에 붙입니다. -------
    """CREATE TABLE IF NOT EXISTS sim_runs (
        batch_id TEXT PRIMARY KEY,
        material TEXT NOT NULL,
        water_l REAL NOT NULL,
        solute_g REAL NOT NULL,
        concentration_g_per_l REAL NOT NULL,
        control_mode TEXT NOT NULL,
        speed_factor REAL NOT NULL,
        fixed_seconds REAL NOT NULL,
        njs_rpm REAL,
        baseline_energy_wh REAL,
        created_at_utc TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sim_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        elapsed_s REAL NOT NULL,
        rpm REAL,
        rpm_setpoint REAL,
        phase TEXT,
        power_w REAL,
        energy_wh REAL,
        cv_pct REAL,
        mix REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sim_metrics_batch_seq ON sim_metrics(batch_id, seq)",
)


def connect(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout={}".format(BUSY_TIMEOUT_MS))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    for stmt in SCHEMA:
        con.execute(stmt)
    con.commit()
    return con


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# batches
# --------------------------------------------------------------------------- #
def batch_exists(con: sqlite3.Connection, batch_id: str) -> bool:
    return con.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone() is not None


def create_batch(con: sqlite3.Connection, batch_id: str, material: str, mode: str,
                 start_trigger: str, scenario: Optional[str], created_at_utc: str,
                 config_json: str, cfg_hash: str, final_status: str) -> None:
    con.execute(
        "INSERT INTO batches (batch_id, material, mode, start_trigger, scenario, created_at_utc, "
        "config_json, config_hash, final_status) VALUES (?,?,?,?,?,?,?,?,?)",
        (batch_id, material, mode, start_trigger, scenario, created_at_utc, config_json,
         cfg_hash, final_status))
    con.commit()


def finish_batch(con: sqlite3.Connection, batch_id: str, final_status: str,
                 finished_at_utc: str) -> None:
    con.execute("UPDATE batches SET final_status=?, finished_at_utc=? WHERE batch_id=?",
                (final_status, finished_at_utc, batch_id))
    con.commit()


def get_batch(con: sqlite3.Connection, batch_id: str) -> Optional[Dict[str, Any]]:
    row = con.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    return dict(row) if row is not None else None


def list_batches(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = con.execute("SELECT batch_id, material, mode, created_at_utc, final_status "
                       "FROM batches ORDER BY created_at_utc").fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# readings / events / decision_log
# --------------------------------------------------------------------------- #
def insert_readings(con: sqlite3.Connection, batch_id: str, seq: int, readings: Iterable[Reading],
                    t_start: float) -> None:
    con.executemany(
        "INSERT INTO readings (batch_id, seq, channel, collected_at_utc, monotonic_s, elapsed_s, "
        "raw_json, value, unit, status, error_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(batch_id, seq, r.channel, r.collected_at_utc, r.monotonic_s,
          round(r.monotonic_s - t_start, 6), r.raw_json, r.value, r.unit, r.status, r.error_detail)
         for r in readings])


def insert_event(con: sqlite3.Connection, batch_id: str, event: EventRecord, at_utc: str,
                 t_start: Optional[float]) -> None:
    elapsed = None if t_start is None else round(event.monotonic_s - t_start, 6)
    con.execute(
        "INSERT INTO events (batch_id, event_type, at_utc, monotonic_s, elapsed_s, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (batch_id, event.event_type, at_utc, event.monotonic_s, elapsed, _dumps(event.detail)))


def insert_decision(con: sqlite3.Connection, batch_id: str, seq: int, monotonic_s: float,
                    t_start: float, state: str, rpm: Optional[float], hold_s: Optional[float],
                    verified: bool, reasons: List[str]) -> None:
    con.execute(
        "INSERT INTO decision_log (batch_id, seq, monotonic_s, elapsed_s, state, rpm, hold_s, "
        "verified, reasons_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (batch_id, seq, monotonic_s, round(monotonic_s - t_start, 6), state, rpm, hold_s,
         1 if verified else 0, _dumps(reasons)))


def list_events(con: sqlite3.Connection, batch_id: str) -> List[Dict[str, Any]]:
    rows = con.execute("SELECT * FROM events WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        out.append(d)
    return out


def list_readings(con: sqlite3.Connection, batch_id: str) -> List[Dict[str, Any]]:
    rows = con.execute("SELECT * FROM readings WHERE batch_id=? ORDER BY seq, id",
                       (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def reading_status_counts(con: sqlite3.Connection, batch_id: str) -> List[Dict[str, Any]]:
    rows = con.execute("SELECT channel, status, count(*) AS n FROM readings WHERE batch_id=? "
                       "GROUP BY channel, status ORDER BY channel, status", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# manual_entries
# --------------------------------------------------------------------------- #
def insert_manual_entry(con: sqlite3.Connection, batch_id: str, kind: str, entered_at_utc: str,
                        position: Optional[str] = None, metric: Optional[str] = None,
                        value: Optional[float] = None, unit: Optional[str] = None,
                        passed: Optional[bool] = None, measured_at_utc: Optional[str] = None,
                        monotonic_s: Optional[float] = None, note: Optional[str] = None) -> int:
    cur = con.execute(
        "INSERT INTO manual_entries (batch_id, kind, position, metric, value, unit, passed, "
        "entered_at_utc, measured_at_utc, monotonic_s, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, kind, position, metric, value, unit,
         None if passed is None else (1 if passed else 0),
         entered_at_utc, measured_at_utc, monotonic_s, note))
    con.commit()
    return int(cur.lastrowid)


def list_manual_entries(con: sqlite3.Connection, batch_id: str) -> List[Dict[str, Any]]:
    rows = con.execute("SELECT * FROM manual_entries WHERE batch_id=? ORDER BY id",
                       (batch_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# sim_runs / sim_metrics (실시간 시뮬레이터)
# --------------------------------------------------------------------------- #
def insert_sim_run(con: sqlite3.Connection, batch_id: str, material: str, params: Any,
                   njs_rpm: float, baseline_energy_wh: float, created_at_utc: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO sim_runs (batch_id, material, water_l, solute_g, "
        "concentration_g_per_l, control_mode, speed_factor, fixed_seconds, njs_rpm, "
        "baseline_energy_wh, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, material, params.water_l, params.solute_g, params.concentration,
         params.control_mode, params.speed_factor, params.fixed_seconds, njs_rpm,
         baseline_energy_wh, created_at_utc))


def insert_sim_metric(con: sqlite3.Connection, batch_id: str, seq: int, m: Dict[str, Any]) -> None:
    con.execute(
        "INSERT INTO sim_metrics (batch_id, seq, elapsed_s, rpm, rpm_setpoint, phase, power_w, "
        "energy_wh, cv_pct, mix) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (batch_id, seq, round(float(m.get("elapsed_s") or 0.0), 6), m.get("rpm"),
         m.get("rpm_setpoint"), m.get("phase"), m.get("power_w"), m.get("energy_wh"),
         m.get("cv_pct"), m.get("mix")))


def get_sim_run(con: sqlite3.Connection, batch_id: str) -> Optional[Dict[str, Any]]:
    row = con.execute("SELECT * FROM sim_runs WHERE batch_id=?", (batch_id,)).fetchone()
    return dict(row) if row is not None else None


def list_sim_metrics(con: sqlite3.Connection, batch_id: str) -> List[Dict[str, Any]]:
    rows = con.execute("SELECT * FROM sim_metrics WHERE batch_id=? ORDER BY seq",
                       (batch_id,)).fetchall()
    return [dict(r) for r in rows]
