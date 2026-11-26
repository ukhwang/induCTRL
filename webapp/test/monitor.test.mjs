import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { normalizeEntry } from '../manual.mjs';
import { createStore } from '../store.mjs';

test('preserves a valid zero, masks a failed reading, and keeps the MIC event timeline', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'stir-monitor-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, 'recordings.sqlite');
  const db = new DatabaseSync(path);
  db.exec(`
    CREATE TABLE batches (batch_id, material, mode, scenario, created_at_utc, finished_at_utc, final_status);
    CREATE TABLE readings (id, batch_id, channel, collected_at_utc, elapsed_s, value, unit, status);
    CREATE TABLE events (id, batch_id, event_type, at_utc, elapsed_s, detail_json);
    CREATE TABLE decision_log (id, batch_id, seq, elapsed_s, state, rpm, hold_s, verified, reasons_json);
    CREATE TABLE manual_entries (id, batch_id, kind, position, metric, value, unit, passed, measured_at_utc, note);

    INSERT INTO batches VALUES ('test','saltwater','mock','normal','2026-01-01T00:00:00Z',NULL,'RUNNING');
    INSERT INTO readings VALUES
      (1,'test','rpm','2026-01-01T00:00:00Z',0,NULL,'rpm','INSUFFICIENT_WINDOW'),
      (2,'test','conductivity','2026-01-01T00:00:01Z',1.15,44,'mS/cm','STALE'),
      (3,'test','rpm','2026-01-01T00:00:02Z',2.11,0,'rpm','OK');
    INSERT INTO events VALUES
      (1,'test','candidate','2026-01-01T00:00:01Z',1.1,'{"verified":false}'),
      (2,'test','stop_confirmed','2026-01-01T00:00:02Z',2.1,'{"method":"rpm"}');
    INSERT INTO decision_log VALUES (1,'test',2,2.15,'NOT_MET',0,0,0,'[]');
  `);
  db.close();

  const original = readFileSync(path);
  const store = createStore({ path });
  const snapshot = await store.snapshot();
  assert.equal(snapshot.mode, 'sqlite');
  assert.equal(snapshot.manual_enabled, false);
  assert.equal(snapshot.live.final_status, 'RUNNING');
  assert.deepEqual(snapshot.live.samples, [
    { elapsed_s: 0, rpm: null },
    { elapsed_s: 1.15, conductivity: null },
    { elapsed_s: 2.11, rpm: 0 },
  ]);
  assert.deepEqual(snapshot.live.latest.conductivity, { value: null, status: 'STALE' });
  assert.equal(snapshot.live.events[0].detail, '미검증 판정입니다.');
  assert.equal(snapshot.live.events[1].code, 'stop_confirmed');
  assert.equal(snapshot.live.elapsed_s, 2.15);
  assert.deepEqual(readFileSync(path), original);
});

test('accepts zero as a sample and limits web entries to records', () => {
  const entry = {
    id: 'aa292fb8-ec32-4005-89dd-2bea53b213f9',
    batchId: 'batch-test',
    kind: 'sample',
    metric: 'temperature',
    value: 0,
  };
  assert.equal(normalizeEntry(entry).value, 0);
  assert.equal(normalizeEntry({ ...entry, value: null }), null);
  assert.equal(normalizeEntry({ ...entry, kind: 'start' }), null);
  assert.equal(normalizeEntry({ ...entry, kind: 'manual_stop' }).value, null);
});
