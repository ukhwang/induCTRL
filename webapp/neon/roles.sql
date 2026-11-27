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
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stir_web_command') THEN
        CREATE ROLE stir_web_command NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stir_mic_agent') THEN
        CREATE ROLE stir_mic_agent NOLOGIN;
    END IF;
END
$roles$;

REVOKE ALL ON SCHEMA stir_monitor FROM PUBLIC;
GRANT USAGE ON SCHEMA stir_monitor TO stir_upload, stir_web_read, stir_web_write,
    stir_web_command, stir_mic_agent;
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
GRANT SELECT ON stir_monitor.stir_commands, stir_monitor.mic_devices TO stir_web_read;
GRANT INSERT ON stir_monitor.web_manual_entries TO stir_web_write;

-- Vercel 서버리스 함수: 센서 조회 + 명령 적재. (한 연결 문자열로 둘 다 하는 전시용 계정)
GRANT SELECT ON stir_monitor.batches, stir_monitor.readings, stir_monitor.events,
    stir_monitor.decision_log, stir_monitor.manual_entries, stir_monitor.upload_health,
    stir_monitor.web_manual_entries, stir_monitor.mic_devices,
    stir_monitor.stir_commands TO stir_web_command;
GRANT INSERT ON stir_monitor.stir_commands TO stir_web_command;
GRANT UPDATE (status, error_message, updated_at) ON stir_monitor.stir_commands TO stir_web_command;
-- id 는 IDENTITY 열이라 INSERT 에 시퀀스 권한이 필요합니다(RETURNING id 는 SELECT 권한도 필요).
GRANT USAGE ON SEQUENCE stir_monitor.stir_commands_id_seq TO stir_web_command;

-- MIC 에이전트: 자기 기기의 명령만 가져가고, 자기 생존 신호만 갱신합니다.
GRANT SELECT ON stir_monitor.stir_commands TO stir_mic_agent;
GRANT UPDATE (status, error_message, result, started_at, finished_at, updated_at)
    ON stir_monitor.stir_commands TO stir_mic_agent;
GRANT USAGE ON SEQUENCE stir_monitor.stir_commands_id_seq TO stir_mic_agent;
GRANT SELECT, INSERT, UPDATE ON stir_monitor.mic_devices TO stir_mic_agent;

COMMIT;
