"""Copy the monitoring projection of a stirctl SQLite database into PostgreSQL."""

import argparse
import fcntl
import json
import logging
import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

LOGGER = logging.getLogger("stir-upload")
CHUNK = 500
HEARTBEAT_SECONDS = 600

BATCH_COLUMNS = (
    "batch_id", "material", "mode", "start_trigger", "scenario",
    "created_at_utc", "finished_at_utc", "final_status",
)
STREAMS = {
    "readings": (
        "id", "batch_id", "seq", "channel", "collected_at_utc", "elapsed_s", "value", "unit", "status",
    ),
    "events": ("id", "batch_id", "event_type", "at_utc", "elapsed_s", "detail_json"),
    "decision_log": ("id", "batch_id", "seq", "elapsed_s", "state", "rpm", "hold_s", "verified", "reasons_json"),
    "manual_entries": (
        "id", "batch_id", "kind", "position", "metric", "value", "unit", "passed",
        "entered_at_utc", "measured_at_utc", "note",
    ),
}


def read_snapshot(path, checkpoint):
    # mode=ro observes the writer's WAL and fails if the database does not exist.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        batches = db.execute("SELECT " + ", ".join(BATCH_COLUMNS) + " FROM batches").fetchall()
        changes = [
            batch for batch in batches
            if checkpoint["batches"].get(batch[0]) != list(batch[-2:])
        ]
        streams = {}
        for table, columns in STREAMS.items():
            streams[table] = db.execute(
                "SELECT " + ", ".join(columns) + " FROM " + table + " WHERE id > ? ORDER BY id LIMIT ?",
                (checkpoint["ids"].get(table, 0), CHUNK),
            ).fetchall()
        return changes, streams


def upload(dsn, source, batches, streams):
    with closing(psycopg2.connect(dsn, connect_timeout=4, application_name="stir-upload")) as connection:
        with connection, connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '8s'")
            # Apply parent rows before child rows. The batch status may be updated on a later poll.
            if batches:
                execute_values(
                    cursor,
                    "INSERT INTO stir_monitor.batches (source_id, " + ", ".join(BATCH_COLUMNS) + ") VALUES %s "
                    "ON CONFLICT (source_id, batch_id) DO UPDATE SET "
                    "finished_at_utc = EXCLUDED.finished_at_utc, final_status = EXCLUDED.final_status",
                    [(source, *row) for row in batches],
                    page_size=CHUNK,
                )
            for table, rows in streams.items():
                if table == "readings":
                    rows = [
                        (*r[:6], r[6] if r[8] == "OK" and r[6] is not None and math.isfinite(r[6]) else None, *r[7:])
                        for r in rows
                    ]
                if rows:
                    execute_values(
                        cursor,
                        "INSERT INTO stir_monitor." + table + " (source_id, " +
                        ", ".join(STREAMS[table]) + ") VALUES %s ON CONFLICT DO NOTHING",
                        [(source, *row) for row in rows],
                        page_size=CHUNK,
                    )
            cursor.execute(
                "INSERT INTO stir_monitor.upload_health (source_id) VALUES (%s) "
                "ON CONFLICT (source_id) DO UPDATE SET updated_at = now()",
                (source,),
            )


def save_checkpoint(path, data):
    fd, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def database_identity(path, source):
    stat = path.stat()
    return [str(path), stat.st_dev, stat.st_ino, source]


def run(args, dsn, source):
    path = args.db.resolve()
    state_path = args.state.resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_name(state_path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state_path.exists():
            checkpoint = json.loads(state_path.read_text())
        else:
            checkpoint = {"identity": None, "batches": {}, "ids": {}, "last_upload": 0}
        retry_delay = 2
        while True:
            try:
                identity = database_identity(path, source)
                if checkpoint["identity"] is not None and checkpoint["identity"] != identity:
                    raise SystemExit("원본 DB 또는 MIC_SOURCE_ID가 변경되었습니다. 새 source ID와 상태 경로를 지정하세요.")
                batches, streams = read_snapshot(path, checkpoint)
                changed = bool(batches or any(streams.values()))
                if changed or time.time() - checkpoint["last_upload"] >= HEARTBEAT_SECONDS:
                    upload(dsn, source, batches, streams)
                    # Progress is durable only after PostgreSQL confirms the transaction.
                    checkpoint["identity"] = identity
                    checkpoint["batches"].update({batch[0]: list(batch[-2:]) for batch in batches})
                    checkpoint["ids"].update({table: rows[-1][0] for table, rows in streams.items() if rows})
                    checkpoint["last_upload"] = time.time()
                    save_checkpoint(state_path, checkpoint)
                    if changed:
                        LOGGER.info("Uploaded batches=%d rows=%d", len(batches), sum(map(len, streams.values())))
                retry_delay = 2
                if any(len(rows) == CHUNK for rows in streams.values()):
                    continue
                if args.once:
                    return
                time.sleep(2)
            except (OSError, sqlite3.Error, psycopg2.Error) as error:
                # Exception messages from a database client can contain connection details.
                LOGGER.warning("Sync retry (%s, SQLSTATE=%s)", type(error).__name__, getattr(error, "pgcode", None))
                if args.once:
                    raise SystemExit(1) from None
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--once", action="store_true", help="Drain pending rows and exit")
    args = parser.parse_args()
    dsn = os.environ.get("NEON_UPLOAD_URL")
    source = os.environ.get("MIC_SOURCE_ID")
    if not dsn or not source:
        parser.error("NEON_UPLOAD_URL and MIC_SOURCE_ID are required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(args, dsn, source)
    except BlockingIOError:
        raise SystemExit("An uploader already holds the checkpoint lock") from None


if __name__ == "__main__":
    main()
