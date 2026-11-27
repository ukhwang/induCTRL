/**
 * 전시장 양방향 제어 — 폰(웹)이 쓰고 MIC 에이전트가 읽는 명령 큐.
 *
 * 흐름
 *   폰 → POST /api/command (이 파일) → Neon stir_monitor.stir_commands (PENDING)
 *   MIC(mic/agent.py) 가 1초마다 PENDING 을 RUNNING 으로 가져가 실제 교반 실행
 *   MIC 가 끝나면 COMPLETED/FAILED 로 마감 → 폰은 /api/command/status 로 확인
 *
 * DB 가 없는 곳(정적 배포, 로컬 SQLite)에서도 같은 인터페이스를 씁니다.
 *   · createCommandService — Neon(PostgreSQL)
 *   · createLocalQueue     — 로컬 서버의 시뮬레이터를 MIC 대신 세운 큐
 */
import { timingSafeEqual } from 'node:crypto';
import pg from 'pg';

const { Pool } = pg;

export const MATERIALS = ['saltwater', 'caco3'];
export const CONTROL_MODES = ['fixed', 'rule', 'ai'];
export const DEFAULT_SOURCE_ID = 'mic-01';
export const DEFAULT_MIC_ONLINE_SECONDS = 20;
export const STALE_PENDING_SECONDS = 60;

const RANGES = {
  water_l: [2, 30],
  solute_g: [50, 2000],
  target_rpm_pct: [10, 100],
};

const MAX_PIN_FAILURES = 5;
const PIN_WINDOW_MS = 60_000;

function number(value, [low, high]) {
  const parsed = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
  if (typeof parsed !== 'number' || !Number.isFinite(parsed)) return null;
  if (parsed < low || parsed > high) return null;
  return parsed;
}

/**
 * 요청 본문을 안전한 명령으로 정리합니다. 잘못된 값이면 null.
 * STOP 은 비상 정지 경로이므로 payload 없이도 받습니다.
 */
export function normalizeCommand(input) {
  if (!input || typeof input !== 'object') return null;
  const action = String(input.action ?? '').trim().toUpperCase();
  if (action === 'STOP') return { action, payload: {} };
  if (action !== 'START') return null;
  const material = MATERIALS.includes(input.material) ? input.material : null;
  const control = input.control_mode ?? input.controlMode ?? 'rule';
  if (!material || !CONTROL_MODES.includes(control)) return null;
  const water = number(input.water_l ?? input.waterL ?? 10, RANGES.water_l);
  const solute = number(input.solute_g ?? input.soluteG ?? 500, RANGES.solute_g);
  const rpm = number(input.target_rpm_pct ?? input.targetRpmPct ?? 60, RANGES.target_rpm_pct);
  if (water === null || solute === null || rpm === null) return null;
  return {
    action,
    payload: {
      material,
      water_l: water,
      solute_g: solute,
      control_mode: control,
      target_rpm_pct: Math.round(rpm),
    },
  };
}

/** 요청자를 payload 에 남기지 않도록 짧은 라벨만 정리합니다(전시장: '목업 폰'). */
export function normalizeRequester(value) {
  if (typeof value !== 'string') return null;
  const text = value.replace(/[\u0000-\u001f<>]/g, '').trim().slice(0, 40);
  return text || null;
}

function pinMatches(provided, expected) {
  const given = Buffer.from(String(provided ?? ''));
  const want = Buffer.from(String(expected));
  return given.length === want.length && timingSafeEqual(given, want);
}

/** PIN 실패 횟수를 프로세스 안에서만 셉니다(서버리스 인스턴스 단위의 방어). */
function createPinLimiter({ max = MAX_PIN_FAILURES, windowMs = PIN_WINDOW_MS } = {}) {
  const failures = new Map();
  const now = () => Date.now();
  return {
    blocked(key) {
      const entry = failures.get(key);
      if (!entry) return false;
      if (now() - entry.first > windowMs) {
        failures.delete(key);
        return false;
      }
      return entry.count >= max;
    },
    fail(key) {
      const entry = failures.get(key);
      if (!entry || now() - entry.first > windowMs) failures.set(key, { count: 1, first: now() });
      else entry.count += 1;
      if (failures.size > 500) {
        for (const [name, value] of failures) if (now() - value.first > windowMs) failures.delete(name);
      }
    },
    reset(key) {
      failures.delete(key);
    },
  };
}

const commandView = (row) => (row ? {
  id: Number(row.id),
  action: row.action,
  status: row.status,
  payload: row.payload ?? {},
  created_at: row.created_at,
  updated_at: row.updated_at,
  started_at: row.started_at ?? null,
  finished_at: row.finished_at ?? null,
  error_message: row.error_message ?? null,
} : null);

const STATE_SQL = `
SELECT
  (SELECT json_build_object(
      'id', d.mic_source_id,
      'online', (now() - d.last_seen_at) <= make_interval(secs => $2::double precision)
                AND coalesce(d.agent_state, '') <> 'stopped',
      'age_s', round(extract(epoch from (now() - d.last_seen_at))::numeric, 1),
      'last_seen_at', d.last_seen_at,
      'state', d.agent_state,
      'stirctl_mode', d.stirctl_mode,
      'active_batch_id', d.active_batch_id,
      'current_command_id', d.current_command_id
    )
   FROM stir_monitor.mic_devices d
   WHERE d.mic_source_id = $1) AS mic,
  (SELECT to_jsonb(c) FROM (
      SELECT id, action, payload, status, created_at, updated_at, started_at, finished_at, error_message
      FROM stir_monitor.stir_commands
      WHERE mic_source_id = $1 AND action = 'START' AND status IN ('PENDING', 'RUNNING')
      ORDER BY id DESC
      LIMIT 1) c) AS active,
  (SELECT to_jsonb(c) FROM (
      SELECT id, status, created_at, updated_at, error_message
      FROM stir_monitor.stir_commands
      WHERE mic_source_id = $1 AND action = 'STOP' AND status IN ('PENDING', 'RUNNING')
      ORDER BY id DESC
      LIMIT 1) c) AS stop,
  (SELECT coalesce(json_agg(row_to_json(r)), '[]'::json) FROM (
      SELECT id, action, status, created_at, updated_at, error_message
      FROM stir_monitor.stir_commands
      WHERE mic_source_id = $1
      ORDER BY id DESC
      LIMIT 5) r) AS recent,
  (SELECT json_build_object(
      'id', b.batch_id, 'material', b.material, 'mode', b.mode,
      'status', b.final_status, 'created_at', b.created_at_utc, 'finished_at', b.finished_at_utc)
   FROM stir_monitor.batches b
   WHERE b.source_id = $1
   ORDER BY b.created_at_utc DESC, b.batch_id DESC
   LIMIT 1) AS batch
`;

/**
 * Neon(PostgreSQL) 명령 큐.
 * @param {object} options
 * @param {string} options.url        명령 큐 접속 문자열(DATABASE_URL)
 * @param {string} options.source     MIC_SOURCE_ID 와 같은 값
 * @param {string} [options.pin]      EXHIBITION_PIN. 비어 있으면 PIN 없이 실행
 * @param {number} [options.micOnlineSeconds] 마지막 생존 신호가 이 시간 안이면 온라인
 * @param {{query:Function}} [options.pool] 테스트용 주입(같은 SQL 을 그대로 씀)
 */
export function createCommandService({
  url,
  source = DEFAULT_SOURCE_ID,
  pin = '',
  micOnlineSeconds = DEFAULT_MIC_ONLINE_SECONDS,
  pool,
} = {}) {
  const db = pool ?? new Pool({
    connectionString: url,
    max: 4,
    connectionTimeoutMillis: 4000,
    idleTimeoutMillis: 8000,
    statement_timeout: 6000,
  });
  const limiter = createPinLimiter();
  const online = Number.isFinite(micOnlineSeconds) && micOnlineSeconds > 0
    ? micOnlineSeconds : DEFAULT_MIC_ONLINE_SECONDS;

  async function state() {
    const { rows } = await db.query(STATE_SQL, [source, online]);
    const row = rows[0] ?? {};
    const active = commandView(row.active);
    return {
      mic: row.mic ?? null,
      active,
      stop: row.stop ? {
        id: Number(row.stop.id),
        status: row.stop.status,
        created_at: row.stop.created_at,
        updated_at: row.stop.updated_at,
        error_message: row.stop.error_message ?? null,
      } : null,
      recent: Array.isArray(row.recent) ? row.recent.map((item) => ({
        id: Number(item.id),
        action: item.action,
        status: item.status,
        created_at: item.created_at,
        updated_at: item.updated_at,
        error_message: item.error_message ?? null,
      })) : [],
      batch: row.batch ?? null,
      running: Boolean(active),
    };
  }

  async function insert(command, requester) {
    try {
      const { rows } = await db.query(
        'INSERT INTO stir_monitor.stir_commands (mic_source_id, action, payload, status, requested_by) '
        + "VALUES ($1, $2, $3::jsonb, 'PENDING', $4) "
        + 'RETURNING id, action, status, created_at',
        [source, command.action, JSON.stringify(command.payload), requester],
      );
      return { row: rows[0], conflict: false };
    } catch (error) {
      // 부분 유니크 인덱스: 이미 진행 중인 START 가 있으면 동시에 눌러도 한 건만 남습니다.
      if (error?.code === '23505') return { row: null, conflict: true };
      throw error;
    }
  }

  async function cancelStale(active) {
    // PENDING 인 채로 1분이 지났으면 에이전트가 집어가지 않은 명령입니다.
    await db.query(
      "UPDATE stir_monitor.stir_commands SET status = 'CANCELED', updated_at = now(), "
      + "error_message = coalesce(error_message, '응답이 없어 자동 취소했습니다.') "
      + "WHERE id = $1 AND mic_source_id = $2 AND status = 'PENDING' "
      + "AND updated_at < now() - make_interval(secs => $3::double precision)",
      [active.id, source, STALE_PENDING_SECONDS],
    );
  }

  /**
   * @returns {{status:number, body:object}} HTTP 응답으로 그대로 내보냅니다.
   */
  async function send(input, { pin: providedPin, client = 'unknown' } = {}) {
    const invalid = { status: 400, body: { ok: false, error: 'invalid', message: '명령 형식이 올바르지 않습니다.' } };
    const command = normalizeCommand(input);
    if (!command) return invalid;

    if (pin) {
      if (limiter.blocked(client)) {
        return { status: 429, body: { ok: false, error: 'rate', message: 'PIN 입력 실패가 많습니다. 잠시 후 다시 시도해 주세요.' } };
      }
      if (!pinMatches(providedPin ?? input?.pin, pin)) {
        limiter.fail(client);
        return { status: 401, body: { ok: false, error: 'pin', message: '운영 PIN 이 필요합니다.' } };
      }
      limiter.reset(client);
    }

    const requester = normalizeRequester(input?.label ?? input?.requested_by);
    const before = await state();

    if (command.action === 'START') {
      if (!before.mic?.online) {
        return {
          status: 503,
          body: {
            ok: false,
            error: 'mic_offline',
            message: 'MIC 가 온라인 상태가 아닙니다. 현장 에이전트(mic/agent.py)를 확인해 주세요.',
            mic: before.mic,
          },
        };
      }
      if (before.active && before.active.action === 'START') {
        await cancelStale(before.active);
        const after = await state();
        if (after.active && after.active.action === 'START') {
          return {
            status: 409,
            body: {
              ok: false,
              error: 'conflict',
              message: '지금 교반이 진행 중입니다. 끝나거나 정지한 뒤에 시작할 수 있습니다.',
              active: after.active,
            },
          };
        }
      }
    }

    const { row, conflict } = await insert(command, requester);
    if (conflict) {
      return {
        status: 409,
        body: {
          ok: false,
          error: 'conflict',
          message: '방금 다른 방문객이 시작했습니다. 현재 운전 중(잠금) 상태입니다.',
          active: (await state()).active,
        },
      };
    }

    return {
      status: 202,
      body: {
        ok: true,
        command: { id: Number(row.id), action: row.action, status: row.status, created_at: row.created_at },
        message: command.action === 'START'
          ? '시작 명령을 보냈습니다. MIC 응답을 기다리는 중입니다.'
          : '정지 명령을 보냈습니다.',
      },
    };
  }

  async function status() {
    const current = await state();
    return {
      mode: 'neon',
      pin_required: Boolean(pin),
      mic_online_seconds: online,
      server_time: new Date().toISOString(),
      ...current,
    };
  }

  return { mode: 'neon', send, status, close: () => db.end?.() };
}

/**
 * 로컬 서버용 큐 — 시뮬레이터(stirctl.py mock)를 MIC 자리에 세웁니다.
 * 전시 전 리허설과 오프라인 데모에서 폰 화면을 그대로 써 보기 위한 것입니다.
 */
export function createLocalQueue({ sim, source = DEFAULT_SOURCE_ID, pin = '', getBatch } = {}) {
  let seq = 0;
  let active = null;
  const stop = null;
  const recent = [];
  const limiter = createPinLimiter();

  const remember = (entry) => {
    recent.unshift(entry);
    while (recent.length > 5) recent.pop();
  };

  function settle(status, error = null) {
    if (!active) return;
    const entry = { ...active, status, updated_at: new Date().toISOString(), error_message: error };
    remember({
      id: entry.id, action: entry.action, status, created_at: entry.created_at,
      updated_at: entry.updated_at, error_message: error,
    });
    active = null;
  }

  function sync() {
    if (!active || active.action !== 'START') return;
    const running = Boolean(sim?.status?.().running);
    if (active.status === 'PENDING' && running) active = { ...active, status: 'RUNNING', started_at: new Date().toISOString() };
    else if (active.status === 'RUNNING' && !running) settle('COMPLETED');
  }

  async function snapshot() {
    const runtime = sim?.status?.() ?? { enabled: false, running: false };
    const batch = getBatch && runtime.batch_id ? await getBatch(runtime.batch_id) : runtime.batch ?? null;
    return {
      mic: sim ? {
        id: source,
        online: true,
        age_s: 0,
        last_seen_at: new Date().toISOString(),
        state: runtime.running ? 'running' : 'idle',
        stirctl_mode: 'mock',
        active_batch_id: runtime.batch_id ?? null,
        current_command_id: active?.id ?? null,
      } : null,
      active,
      stop,
      recent,
      batch: batch ? { id: batch.id, material: batch.material, status: batch.final_status, created_at: batch.created_at } : null,
      running: Boolean(active),
    };
  }

  async function send(input, { pin: providedPin, client = 'unknown' } = {}) {
    const command = normalizeCommand(input);
    if (!command) return { status: 400, body: { ok: false, error: 'invalid', message: '명령 형식이 올바르지 않습니다.' } };
    if (!sim) {
      return { status: 503, body: { ok: false, error: 'control_disabled', message: '이 서버에는 시뮬레이터가 없습니다.' } };
    }
    if (pin) {
      if (limiter.blocked(client)) {
        return { status: 429, body: { ok: false, error: 'rate', message: 'PIN 입력 실패가 많습니다. 잠시 후 다시 시도해 주세요.' } };
      }
      if (!pinMatches(providedPin ?? input?.pin, pin)) {
        limiter.fail(client);
        return { status: 401, body: { ok: false, error: 'pin', message: '운영 PIN 이 필요합니다.' } };
      }
      limiter.reset(client);
    }
    sync();
    if (command.action === 'START') {
      if (active) {
        return { status: 409, body: { ok: false, error: 'conflict', message: '이미 운전 중입니다(잠금).', active } };
      }
      const started = await sim.start(command.payload);
      if (!started.ok) {
        return { status: 400, body: { ok: false, error: started.error ?? 'start_failed', message: started.message ?? '시작하지 못했습니다.' } };
      }
      seq += 1;
      active = {
        id: seq, action: 'START', status: 'PENDING', payload: command.payload,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        started_at: null, finished_at: null, error_message: null,
      };
      sync();
      return { status: 202, body: { ok: true, command: { id: seq, action: 'START', status: 'RUNNING' }, message: '로컬 시뮬레이터를 시작했습니다.' } };
    }
    if (active) settle('CANCELED', '정지 요청으로 취소');
    await sim.stop();
    seq += 1;
    const completedAt = new Date().toISOString();
    remember({ id: seq, action: 'STOP', status: 'COMPLETED', created_at: completedAt, updated_at: completedAt, error_message: null });
    return { status: 202, body: { ok: true, command: { id: seq, action: 'STOP', status: 'COMPLETED' }, message: '로컬 시뮬레이터를 정지했습니다.' } };
  }

  async function status() {
    sync();
    return {
      mode: 'local',
      pin_required: Boolean(pin),
      mic_online_seconds: 0,
      server_time: new Date().toISOString(),
      ...(await snapshot()),
    };
  }

  return { mode: 'local', send, status };
}
