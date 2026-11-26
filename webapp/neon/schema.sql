BEGIN;

CREATE SCHEMA IF NOT EXISTS stir_monitor;
SET LOCAL search_path = stir_monitor, pg_catalog;

CREATE TABLE IF NOT EXISTS batches (
    source_id text NOT NULL,
    batch_id text NOT NULL,
    material text NOT NULL,
    mode text NOT NULL,
    start_trigger text NOT NULL,
    scenario text,
    created_at_utc timestamptz NOT NULL,
    finished_at_utc timestamptz,
    final_status text NOT NULL,
    PRIMARY KEY (source_id, batch_id)
);

CREATE TABLE IF NOT EXISTS readings (
    source_id text NOT NULL,
    id bigint NOT NULL,
    batch_id text NOT NULL,
    seq bigint NOT NULL,
    channel text NOT NULL,
    collected_at_utc timestamptz NOT NULL,
    elapsed_s double precision NOT NULL,
    value double precision,
    unit text,
    status text NOT NULL,
    PRIMARY KEY (source_id, id),
    FOREIGN KEY (source_id, batch_id) REFERENCES batches (source_id, batch_id)
);

CREATE TABLE IF NOT EXISTS events (
    source_id text NOT NULL,
    id bigint NOT NULL,
    batch_id text NOT NULL,
    event_type text NOT NULL,
    at_utc timestamptz NOT NULL,
    elapsed_s double precision,
    detail_json text,
    PRIMARY KEY (source_id, id),
    FOREIGN KEY (source_id, batch_id) REFERENCES batches (source_id, batch_id)
);

CREATE TABLE IF NOT EXISTS decision_log (
    source_id text NOT NULL,
    id bigint NOT NULL,
    batch_id text NOT NULL,
    seq bigint NOT NULL,
    elapsed_s double precision NOT NULL,
    state text NOT NULL,
    rpm double precision,
    hold_s double precision,
    verified integer,
    reasons_json text,
    PRIMARY KEY (source_id, id),
    FOREIGN KEY (source_id, batch_id) REFERENCES batches (source_id, batch_id)
);

CREATE TABLE IF NOT EXISTS manual_entries (
    source_id text NOT NULL,
    id bigint NOT NULL,
    batch_id text NOT NULL,
    kind text NOT NULL,
    position text,
    metric text,
    value double precision,
    unit text,
    passed integer,
    entered_at_utc timestamptz NOT NULL,
    measured_at_utc timestamptz,
    note text,
    PRIMARY KEY (source_id, id),
    FOREIGN KEY (source_id, batch_id) REFERENCES batches (source_id, batch_id)
);

CREATE TABLE IF NOT EXISTS upload_health (
    source_id text PRIMARY KEY,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS web_manual_entries (
    source_id text NOT NULL,
    id uuid NOT NULL,
    batch_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('sample', 'quality', 'manual_stop')),
    position text,
    metric text,
    value double precision,
    unit text,
    passed integer CHECK (passed IN (0, 1)),
    entered_at_utc timestamptz NOT NULL DEFAULT now(),
    measured_at_utc timestamptz NOT NULL DEFAULT now(),
    note text,
    PRIMARY KEY (source_id, id),
    FOREIGN KEY (source_id, batch_id) REFERENCES batches (source_id, batch_id),
    CHECK (
        (kind = 'sample' AND metric IS NOT NULL AND value IS NOT NULL AND passed IS NULL)
        OR (kind = 'quality' AND passed IS NOT NULL AND metric IS NULL AND value IS NULL)
        OR (kind = 'manual_stop' AND passed IS NULL AND metric IS NULL AND value IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS batches_recent ON batches (source_id, created_at_utc DESC);
CREATE INDEX IF NOT EXISTS readings_batch_timeline ON readings (source_id, batch_id, elapsed_s, id);
CREATE INDEX IF NOT EXISTS events_batch ON events (source_id, batch_id, id);
CREATE INDEX IF NOT EXISTS decision_batch ON decision_log (source_id, batch_id, seq, id);
CREATE INDEX IF NOT EXISTS manual_batch ON manual_entries (source_id, batch_id, id);
CREATE INDEX IF NOT EXISTS web_manual_batch ON web_manual_entries (source_id, batch_id, entered_at_utc);

COMMIT;
