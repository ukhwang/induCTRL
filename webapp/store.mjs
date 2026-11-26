import { stat } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';
import pg from 'pg';

const { Pool } = pg;
const displayChannels = ['rpm', 'conductivity', 'turbidity_top', 'turbidity_bottom'];
const labels = {
  run_started: '수집 시작',
  batch_started: '교반 시작',
  warning: '경고',
  overrun: '수집 주기 초과',
  COUNTER_RESET: '회전 카운터 초기화',
  counter_gap: '회전 카운터 데이터 누락',
  hold_reset: '유지시간 초기화',
  candidate: '교반 완료 후보',
  stop_blocked: '정지 요청 차단',
  stop_requested: 'MIC 정지 요청 기록',
  modbus_response: '정지 요청 응답 기록',
  stop_confirmed: '정지 확인',
  stop_unconfirmed: '정지 미확인',
  stop_observed: '정지 관측',
  run_finished: '수집 종료',
  error: '수집 오류',
};

const batchColumns = 'batch_id, material, mode, scenario, created_at_utc, finished_at_utc, final_status';
const manualColumns = 'id, kind, position, metric, value, unit, passed, measured_at_utc, note';

function query(table, columns, conditions, source, tail) {
  const entries = Object.entries(conditions);
  if (source) entries.unshift(['source_id', source]);
  const where = entries.map(([key], index) => `${key} = $${index + 1}`);
  return [
    `SELECT ${columns} FROM ${source ? 'stir_monitor.' : ''}${table}${where.length ? ' WHERE ' + where.join(' AND ') : ''} ${tail}`,
    entries.map(([, value]) => value),
  ];
}

function postgresPool(url) {
  return new Pool({
    connectionString: url,
    max: 3,
    connectionTimeoutMillis: 4000,
    idleTimeoutMillis: 8000,
    statement_timeout: 6000,
  });
}

function reader({ path, url, source }) {
  if (url) {
    const pool = postgresPool(url);
    pool.on('error', () => console.error('Neon reader connection interrupted'));
    return async (work) => {
      const client = await pool.connect();
      try {
        await client.query('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY');
        const result = await work(async (table, columns, conditions = {}, tail = '') => {
          const [sql, parameters] = query(table, columns, conditions, source, tail);
          return (await client.query(sql, parameters)).rows;
        });
        await client.query('COMMIT');
        return result;
      } catch (error) {
        await client.query('ROLLBACK').catch(() => {});
        throw error;
      } finally {
        client.release();
      }
    };
  }

  return async (work) => {
    await stat(path);
    const db = new DatabaseSync(path, { readOnly: true, timeout: 1500 });
    try {
      db.exec('PRAGMA query_only=ON; BEGIN');
      return await work(async (table, columns, conditions = {}, tail = '') => {
        const [sql, parameters] = query(table, columns, conditions, null, tail);
        const named = Object.fromEntries(parameters.map((value, index) => [`$${index + 1}`, value]));
        return db.prepare(sql).all(named);
      });
    } finally {
      db.close();
    }
  };
}

function baseBatch(row) {
  return {
    id: row.batch_id,
    material: row.material,
    mode: row.mode,
    scenario: row.scenario,
    created_at: row.created_at_utc,
    final_status: row.final_status,
  };
}

function eventDetail(row) {
  let details = {};
  try {
    details = JSON.parse(row.detail_json ?? '{}') || {};
  } catch {
    // An unreadable detail cannot verify a candidate.
  }
  if (row.event_type === 'candidate') {
    return details.verified === true ? '검증된 판정 기준을 충족했습니다.' : '미검증 판정입니다.';
  }
  if (row.event_type === 'stop_confirmed') {
    return details.method === 'rpm' ? '회전수로 정지를 확인했습니다.' : 'MIC에서 정지를 확인했습니다.';
  }
  return '';
}

async function getRun(rows, batch, cloud) {
  const filters = { batch_id: batch.batch_id };
  const readings = await rows('readings', 'channel, collected_at_utc, elapsed_s, value, unit, status',
    filters, "AND channel IN ('rpm', 'conductivity', 'turbidity_top', 'turbidity_bottom') ORDER BY elapsed_s, id");
  const events = await rows('events', 'event_type, at_utc, elapsed_s, detail_json', filters, 'ORDER BY id');
  const decisions = await rows('decision_log', 'elapsed_s, state, rpm, hold_s, verified, reasons_json',
    filters, 'ORDER BY seq DESC, id DESC LIMIT 1');
  const micEntries = (await rows('manual_entries', manualColumns, filters, 'ORDER BY id DESC LIMIT 100'))
    .map((row) => ({ ...row, source: 'MIC' }));
  const webEntries = cloud
    ? (await rows('web_manual_entries', manualColumns, filters, 'ORDER BY entered_at_utc DESC LIMIT 100'))
      .map((row) => ({ ...row, source: '웹' }))
    : [];
  const samples = [];
  const latest = {};
  const units = {};
  let lastReadingAt = null;
  for (const reading of readings) {
    const value = reading.status === 'OK' && Number.isFinite(reading.value) ? reading.value : null;
    samples.push({ elapsed_s: reading.elapsed_s, [reading.channel]: value });
    latest[reading.channel] = { value, status: reading.status };
    units[reading.channel] = reading.unit || units[reading.channel] || '';
    const timestamp = new Date(reading.collected_at_utc).getTime();
    if (Number.isFinite(timestamp) && (lastReadingAt === null || lastReadingAt < timestamp)) lastReadingAt = timestamp;
  }
  const metrics = displayChannels.filter((key) => Object.hasOwn(latest, key));
  const lastEventTime = events.reduce((latest, row) => Math.max(latest, row.elapsed_s ?? 0), 0);
  const elapsed = Math.max(0, decisions[0]?.elapsed_s ?? 0, samples.at(-1)?.elapsed_s ?? 0, lastEventTime);
  return {
    ...baseBatch(batch),
    elapsed_s: elapsed,
    duration_s: elapsed,
    current_decision: decisions[0] ?? null,
    metrics,
    latest,
    units,
    last_reading_at: lastReadingAt === null ? null : new Date(lastReadingAt).toISOString(),
    samples,
    events: events.map((row) => ({
      code: row.event_type,
      elapsed_s: row.elapsed_s,
      title: labels[row.event_type] ?? row.event_type,
      detail: eventDetail(row),
    })),
    entries: [...micEntries, ...webEntries].sort((a, b) => new Date(b.measured_at_utc) - new Date(a.measured_at_utc)),
  };
}

export function createStore({ path, url, source, writeUrl }) {
  const read = reader({ path, url, source });
  const writer = writeUrl ? postgresPool(writeUrl) : null;
  writer?.on('error', () => console.error('Neon record writer connection interrupted'));
  return {
    async snapshot() {
      return read(async (rows) => {
        const batches = await rows('batches', batchColumns, {}, 'ORDER BY created_at_utc DESC, batch_id DESC LIMIT 9');
        const health = url ? await rows('upload_health', 'updated_at') : [];
        const live = batches[0] ? await getRun(rows, batches[0], Boolean(url)) : null;
        return {
          mode: url ? 'neon' : 'sqlite',
          received_at: health[0]?.updated_at ?? live?.last_reading_at ?? null,
          manual_enabled: Boolean(writer),
          live,
          recordings: batches
            .filter((batch) => batch.finished_at_utc !== null)
            .slice(0, 8).map(baseBatch),
        };
      });
    },
    async batch(id) {
      return read(async (rows) => {
        const [batch] = await rows('batches', batchColumns, { batch_id: id });
        return batch ? getRun(rows, batch, Boolean(url)) : null;
      });
    },
    async manual(entry) {
      const values = [source, entry.id, entry.batchId, entry.kind, entry.position,
        entry.metric, entry.value, entry.unit, entry.passed, entry.measuredAt, entry.note];
      await writer.query(
        'INSERT INTO stir_monitor.web_manual_entries ' +
        '(source_id, id, batch_id, kind, position, metric, value, unit, passed, measured_at_utc, note) ' +
        'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,COALESCE($10::timestamptz, now()),$11) ON CONFLICT DO NOTHING',
        values,
      );
    },
  };
}
