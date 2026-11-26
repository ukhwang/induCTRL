/**
 * 실시간 시뮬레이터 프로세스 관리.
 *
 * 웹 화면의 '실험 시작'이 stirctl.py run --mode mock 을 자식 프로세스로 띄웁니다.
 * 모의(mock) 모드만 실행하며 Modbus 쓰기는 하지 않습니다. 한 번에 하나만 돌립니다.
 */
import { spawn as spawnProcess } from 'node:child_process';

export const MATERIALS = ['saltwater', 'caco3'];
export const CONTROL_MODES = ['fixed', 'rule', 'ai'];
export const LABELS = { saltwater: '소금물 실험', caco3: '탄산칼슘 실험' };

const RANGES = {
  water_l: [0.5, 200],
  solute_g: [0, 20000],
  speed_factor: [0.25, 50],
};

const number = (value, range) => {
  const parsed = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
  if (typeof parsed !== 'number' || !Number.isFinite(parsed)) return null;
  if (parsed < range[0] || parsed > range[1]) return null;
  return parsed;
};

/** 요청 본문을 안전한 실험 조건으로 정리합니다. 잘못된 값이면 null. */
export function normalizeSimConfig(input) {
  if (!input || typeof input !== 'object') return null;
  const material = MATERIALS.includes(input.material) ? input.material : null;
  if (!material) return null;
  const control = input.control_mode ?? input.controlMode ?? 'rule';
  if (!CONTROL_MODES.includes(control)) return null;
  const water = number(input.water_l ?? input.waterL ?? 10, RANGES.water_l);
  const solute = number(input.solute_g ?? input.soluteG ?? 500, RANGES.solute_g);
  const speed = number(input.speed_factor ?? input.speedFactor ?? 5, RANGES.speed_factor);
  if (water === null || solute === null || speed === null) return null;
  return { material, water_l: water, solute_g: solute, control_mode: control, speed_factor: speed };
}

function batchIdFor({ material }) {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  const stamp = `${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const salt = Math.random().toString(16).slice(2, 6);
  return `sim-${material}-${stamp}-${salt}`;
}

export function buildArgs(config, batchId, dbPath) {
  return [
    'stirctl.py', 'run',
    '--mode', 'mock',
    '--material', config.material,
    '--batch-id', batchId,
    '--db', dbPath,
    '--control-mode', config.control_mode,
    '--water-l', String(config.water_l),
    '--solute-g', String(config.solute_g),
    '--speed-factor', String(config.speed_factor),
    '--status-every', '0',
  ];
}

export function createSimJob({
  dbPath,
  cwd,
  python = process.env.SIM_PYTHON || 'python3',
  script = 'stirctl.py',
  maxLog = 60,
  spawnImpl = spawnProcess,
} = {}) {
  let child = null;
  let current = null;
  const log = [];

  const record = (line) => {
    const text = String(line).trimEnd();
    if (!text) return;
    log.push(text);
    while (log.length > maxLog) log.shift();
  };

  const alive = () => Boolean(child);

  function finish(code, signal) {
    if (current) current.exit = { code, signal, at: new Date().toISOString() };
    child = null;
  }

  function terminate(target) {
    if (!target) return;
    try {
      target.kill('SIGINT');           // KeyboardInterrupt 경로로 배치를 INTERRUPTED 로 마감
    } catch {
      // 이미 끝난 프로세스
    }
    const timer = setTimeout(() => {
      try {
        target.kill('SIGKILL');
      } catch {
        // 무시
      }
    }, 3000);
    timer.unref?.();
    target.once('exit', () => clearTimeout(timer));
  }

  async function stop() {
    const target = child;
    if (!target) return { ok: true, stopped: false };
    terminate(target);
    await new Promise((resolve) => {
      if (target.exitCode !== null || target.signalCode) return resolve();
      const done = () => resolve();
      target.once('exit', done);
      setTimeout(done, 4000).unref?.();
    });
    if (child === target) child = null;
    return { ok: true, stopped: true };
  }

  async function start(raw) {
    const config = normalizeSimConfig(raw);
    if (!config) return { ok: false, error: 'invalid', message: '실험 조건이 올바르지 않습니다.' };
    await stop();
    const batchId = batchIdFor(config);
    const args = buildArgs(config, batchId, dbPath);
    let proc;
    try {
      proc = spawnImpl(python, args, {
        cwd,
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      record(`${error.name}: ${error.message}`);
      return { ok: false, error: 'spawn_failed', message: '시뮬레이터를 실행하지 못했습니다.' };
    }
    child = proc;
    current = {
      batch_id: batchId,
      config,
      started_at: new Date().toISOString(),
      pid: proc.pid ?? null,
      exit: null,
      db: dbPath,
      argv: args,
    };
    proc.stdout?.setEncoding?.('utf8');
    proc.stderr?.setEncoding?.('utf8');
    proc.stdout?.on('data', (chunk) => String(chunk).split('\n').forEach(record));
    proc.stderr?.on('data', (chunk) => String(chunk).split('\n').forEach(record));

    const outcome = await new Promise((resolve) => {
      let settled = false;
      const settle = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };
      proc.once('spawn', () => settle({ ok: true }));
      proc.once('error', (error) => {
        record(`${error.code ?? error.name}: ${error.message}`);
        finish(null, null);
        settle({
          ok: false,
          error: error.code === 'ENOENT' ? 'python_unavailable' : (error.code ?? 'spawn_failed'),
          message: error.code === 'ENOENT'
            ? 'python3 를 찾지 못해 브라우저 엔진으로 대체합니다.'
            : '시뮬레이터를 실행하지 못했습니다.',
        });
      });
      setTimeout(() => settle({ ok: true }), 1500).unref?.();
    });
    proc.once('exit', (code, signal) => {
      record(`종료: code=${code ?? '-'} signal=${signal ?? '-'}`);
      finish(code, signal);
    });
    if (!outcome.ok) return outcome;
    return { ok: true, batch_id: batchId, config, db: dbPath };
  }

  function status() {
    return {
      enabled: true,
      running: alive(),
      batch_id: current?.batch_id ?? null,
      config: current?.config ?? null,
      started_at: current?.started_at ?? null,
      pid: current?.pid ?? null,
      elapsed_s: alive() && current ? (Date.now() - Date.parse(current.started_at)) / 1000 : null,
      exit: current?.exit ?? null,
      log: log.slice(-12),
    };
  }

  return { start, stop, status, get batch() { return current; } };
}
