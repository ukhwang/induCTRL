BEGIN;

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stir_upload') THEN
        CREATE ROLE stir_upload NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stir_web_read') THEN
        CREATE ROLE stir_web_read NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stir_web_write') THEN
        CREATE ROLE stir_web_write NOLOGIN;
    END IF;
END
$roles$;

REVOKE ALL ON SCHEMA stir_monitor FROM PUBLIC;
GRANT USAGE ON SCHEMA stir_monitor TO stir_upload, stir_web_read, stir_web_write;
GRANT INSERT ON stir_monitor.batches TO stir_upload;
GRANT SELECT (source_id, batch_id, finished_at_utc, final_status) ON stir_monitor.batches TO stir_upload;
GRANT UPDATE (finished_at_utc, final_status) ON stir_monitor.batches TO stir_upload;
GRANT INSERT ON stir_monitor.readings, stir_monitor.events,
    stir_monitor.decision_log, stir_monitor.manual_entries TO stir_upload;
GRANT INSERT ON stir_monitor.upload_health TO stir_upload;
GRANT SELECT (source_id) ON stir_monitor.upload_health TO stir_upload;
GRANT UPDATE (updated_at) ON stir_monitor.upload_health TO stir_upload;

GRANT SELECT ON stir_monitor.batches, stir_monitor.readings, stir_monitor.events,
    stir_monitor.decision_log, stir_monitor.manual_entries, stir_monitor.upload_health,
    stir_monitor.web_manual_entries TO stir_web_read;
GRANT INSERT ON stir_monitor.web_manual_entries TO stir_web_write;

COMMIT;
