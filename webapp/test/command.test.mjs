import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createCommandService, createLocalQueue, normalizeCommand } from '../commands.mjs';

/**
 * 실제 SQL 은 그대로 두고, DB 만 흉내 냅니다.
 * (부분 유니크 인덱스·json 집계는 PostgreSQL 이 하지만 여기서는 규칙만 검증합니다.)
 */
function fakeDb({ micAge = 1, commands = [] } = {}) {
  const rows = commands.map((row) => ({
    id: row.id, action: row.action, payload: row.payload ?? {}, status: row.status ?? 'PENDING',
    created_at: row.created_at ?? new Date().toISOString(),
    updated_at: row.updated_at ?? new Date().toISOString(),
    started_at: null, finished_at: null, error_message: null,
  }));
  let nextId = rows.reduce((max, row) => Math.max(max, row.id), 0) + 1;
  const activeStart = () => [...rows].sort((a, b) => b.id - a.id)
    .find((row) => row.action === 'START' && row.status !== 'COMPLETED' && row.status !== 'FAILED' && row.status !== 'CANCELED'
      && row.status !== 'CANCELED');
  const online = { age: micAge };
  const db = {
    rows,
    online,
    queries: [],
    async query(sql, params) {
      db.queries.push({ sql, params });
      if (sql.includes('json_build_object')) {
        const [, micOnlineSeconds] = params;
        const start = activeStart();
        return {
          rows: [{
            mic: {
              id: params[0],
              online: online.age <= micOnlineSeconds && online.state !== 'stopped',
              age_s: online.age,
              last_seen_at: new Date().toISOString(),
              state: online.state ?? 'idle',
              stirctl_mode: 'observe',
              active_batch_id: null,
              current_command_id: start?.id ?? null,
            },
            active: start ? { ...start } : null,
            stop: null,
            recent: [...rows].sort((a, b) => b.id - a.id).slice(0, 5)
              .map(({ id, action, status, created_at, updated_at, error_message }) => (
                { id, action, status, created_at, updated_at, error_message })),
            batch: null,
          }],
        };
      }
      if (sql.startsWith('INSERT INTO stir_monitor.stir_commands')) {
        const [, action, payload, requestedBy] = params;
        if (action === 'START' && activeStart()) {
          const error = new Error('duplicate key value violates unique constraint');
          error.code = '23505';
          throw error;
        }
        const row = {
          id: nextId++, action, payload: JSON.parse(payload), status: 'PENDING',
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
          started_at: null, finished_at: null, error_message: null, requested_by: requestedBy,
        };
        rows.push(row);
        return { rows: [{ id: row.id, action, status: 'PENDING', created_at: row.created_at }] };
      }
      if (sql.includes("SET status = 'CANCELED'") && sql.includes('make_interval')) {
        const [id, , seconds] = params;
        const row = rows.find((item) => item.id === id);
        const old = row && Date.now() - Date.parse(row.updated_at) > seconds * 1000;
        if (row && row.status === 'PENDING' && old) row.status = 'CANCELED';
        return { rows: [] };
      }
      throw new Error(`unexpected sql: ${sql.slice(0, 70)}`);
    },
  };
  return db;
}

const start = { action: 'START', material: 'saltwater', water_l: 10, solute_g: 500, control_mode: 'rule', target_rpm_pct: 60 };

test('normalizes a start command and rejects unsafe values', () => {
  assert.deepEqual(normalizeCommand(start), {
    action: 'START',
    payload: { material: 'saltwater', water_l: 10, solute_g: 500, control_mode: 'rule', target_rpm_pct: 60 },
  });
  assert.equal(normalizeCommand({ action: 'START', material: 'saltwater' }).payload.water_l, 10);
  assert.equal(normalizeCommand({ action: 'start', material: 'caco3', water_l: '8', solute_g: '300', target_rpm_pct: '45' })
    .payload.target_rpm_pct, 45);
  assert.deepEqual(normalizeCommand({ action: 'STOP' }), { action: 'STOP', payload: {} });
  assert.equal(normalizeCommand({ action: 'START', material: 'cement' }), null);
  assert.equal(normalizeCommand({ action: 'START', material: 'saltwater', water_l: 500 }), null);
  assert.equal(normalizeCommand({ action: 'START', material: 'saltwater', target_rpm_pct: 0 }), null);
  assert.equal(normalizeCommand({ action: 'REBOOT' }), null);
  assert.equal(normalizeCommand(null), null);
});

test('queues a start command and returns the command id', async () => {
  const db = fakeDb();
  const service = createCommandService({ source: 'mic-01', pool: db });
  const result = await service.send(start, {});
  assert.equal(result.status, 202);
  assert.equal(result.body.ok, true);
  assert.equal(result.body.command.action, 'START');
  assert.equal(db.rows.length, 1);
  assert.equal(db.rows[0].status, 'PENDING');
  assert.equal(db.rows[0].payload.target_rpm_pct, 60);
});

test('refuses a second start while a batch is running', async () => {
  const db = fakeDb({ commands: [{ id: 1, action: 'START', status: 'RUNNING' }] });
  const service = createCommandService({ source: 'mic-01', pool: db });
  const result = await service.send(start, {});
  assert.equal(result.status, 409);
  assert.equal(result.body.error, 'conflict');
  assert.equal(result.body.active.id, 1);
  assert.equal(db.rows.length, 1);
});

test('a concurrent second press also lands on 409 through the unique index', async () => {
  const db = fakeDb();
  const service = createCommandService({ source: 'mic-01', pool: db });
  // 첫 요청이 상태를 읽은 뒤, 다른 방문객이 먼저 넣은 상황을 만듭니다.
  const [first, second] = await Promise.all([service.send(start, {}), service.send({ ...start, material: 'caco3' }, {})]);
  const statuses = [first.status, second.status].sort();
  assert.deepEqual(statuses, [202, 409]);
  assert.equal(db.rows.length, 1);
});

test('rejects start when the MIC has not sent a heartbeat', async () => {
  const db = fakeDb({ micAge: 45 });
  const service = createCommandService({ source: 'mic-01', pool: db, micOnlineSeconds: 20 });
  const result = await service.send(start, {});
  assert.equal(result.status, 503);
  assert.equal(result.body.error, 'mic_offline');
  assert.equal(db.rows.length, 0);
});

test('stop is always accepted, even with nothing running and the MIC offline', async () => {
  const db = fakeDb({ micAge: 600 });
  const service = createCommandService({ source: 'mic-01', pool: db, micOnlineSeconds: 20 });
  const result = await service.send({ action: 'STOP' }, {});
  assert.equal(result.status, 202);
  assert.equal(db.rows.length, 1);
  assert.equal(db.rows[0].action, 'STOP');
});

test('a stale pending start is canceled so the fair can start again', async () => {
  const stale = {
    id: 1, action: 'START', status: 'PENDING',
    created_at: new Date(Date.now() - 120_000).toISOString(),
    updated_at: new Date(Date.now() - 120_000).toISOString(),
  };
  const db = fakeDb({ commands: [stale] });
  const service = createCommandService({ source: 'mic-01', pool: db });
  const result = await service.send(start, {});
  assert.equal(db.rows[0].status, 'CANCELED');
  assert.equal(result.status, 202);
});

test('exhibition pin is required once configured, and repeated failures are throttled', async () => {
  const db = fakeDb();
  const service = createCommandService({ source: 'mic-01', pool: db, pin: '2468' });
  assert.equal((await service.send(start, { client: 'a' })).status, 401);
  assert.equal((await service.send(start, { pin: '1111', client: 'a' })).status, 401);
  const ok = await service.send({ ...start, pin: '2468' }, { client: 'a' });
  assert.equal(ok.status, 202);

  const blocked = createCommandService({ source: 'mic-01', pool: fakeDb(), pin: '2468' });
  for (let attempt = 0; attempt < 5; attempt += 1) {
    assert.equal((await blocked.send(start, { pin: '0000', client: 'b' })).status, 401);
  }
  assert.equal((await blocked.send(start, { pin: '2468', client: 'b' })).status, 429);
});

test('status reports device, active command and history', async () => {
  const db = fakeDb({ commands: [{ id: 3, action: 'START', status: 'RUNNING' }, { id: 2, action: 'STOP', status: 'COMPLETED' }] });
  const service = createCommandService({ source: 'mic-01', pool: db, pin: '2468' });
  const status = await service.status();
  assert.equal(status.mode, 'neon');
  assert.equal(status.pin_required, true);
  assert.equal(status.mic.online, true);
  assert.equal(status.active.id, 3);
  assert.equal(status.running, true);
  assert.deepEqual(status.recent.map((row) => row.id), [3, 2]);
});

test('local queue mirrors the same interface on top of the simulator', async () => {
  const calls = [];
  let running = false;
  const sim = {
    status: () => ({ enabled: true, running, batch_id: running ? 'sim-1' : null }),
    start: async (payload) => { calls.push(['start', payload]); running = true; return { ok: true, batch_id: 'sim-1' }; },
    stop: async () => { calls.push(['stop']); running = false; return { ok: true, stopped: true }; },
  };
  const queue = createLocalQueue({ sim });
  const started = await queue.send(start, {});
  assert.equal(started.status, 202);
  assert.equal(calls[0][0], 'start');
  const status = await queue.status();
  assert.equal(status.mode, 'local');
  assert.equal(status.running, true);
  assert.equal(status.mic.online, true);
  assert.equal((await queue.send(start, {})).status, 409);

  const stopped = await queue.send({ action: 'STOP' }, {});
  assert.equal(stopped.status, 202);
  assert.equal((await queue.status()).running, false);
  assert.equal(calls.at(-1)[0], 'stop');
});

test('snapshot falls back to the simulator when no database is configured', async () => {
  const { snapshotResponse } = await import('../apilib.mjs');
  const saved = { ...process.env };
  delete process.env.DATABASE_URL;
  delete process.env.NEON_READ_URL;
  try {
    const { status, body } = await snapshotResponse();
    assert.equal(status, 200);
    assert.equal(body.mode, 'sim_only');
    assert.equal(body.control_enabled, false);
  } finally {
    for (const key of ['DATABASE_URL', 'NEON_READ_URL']) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  }
});
