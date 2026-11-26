import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import { buildArgs, createSimJob, normalizeSimConfig } from '../sim.mjs';

function fakeChild() {
  const child = new EventEmitter();
  child.pid = 4242;
  child.exitCode = null;
  child.signalCode = null;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.signals = [];
  child.kill = (signal) => {
    child.signals.push(signal);
    if (signal === 'SIGINT' || signal === 'SIGKILL') {
      child.exitCode = 0;
      child.emit('exit', 0, signal);
    }
    return true;
  };
  return child;
}

test('normalizes and rejects simulator input', () => {
  assert.deepEqual(normalizeSimConfig({ material: 'saltwater', water_l: 10, solute_g: 500, control_mode: 'ai' }),
    { material: 'saltwater', water_l: 10, solute_g: 500, control_mode: 'ai', speed_factor: 5 });
  assert.equal(normalizeSimConfig({ material: 'saltwater' }).control_mode, 'rule');
  assert.equal(normalizeSimConfig({ material: 'alcohol' }), null);
  assert.equal(normalizeSimConfig({ material: 'caco3', control_mode: 'auto' }), null);
  assert.equal(normalizeSimConfig({ material: 'caco3', water_l: 0 }), null);
  assert.equal(normalizeSimConfig({ material: 'caco3', solute_g: 99999 }), null);
  assert.equal(normalizeSimConfig({ material: 'caco3', speed_factor: 200 }), null);
  assert.equal(normalizeSimConfig(null), null);
});

test('builds the stirctl mock command', () => {
  const args = buildArgs({ material: 'caco3', control_mode: 'ai', water_l: 12, solute_g: 800, speed_factor: 10 },
    'sim-caco3-x', '/tmp/db.sqlite');
  assert.equal(args[0], 'stirctl.py');
  assert.equal(args[1], 'run');
  assert.ok(args.includes('mock'));
  assert.equal(args[args.indexOf('--db') + 1], '/tmp/db.sqlite');
  assert.equal(args[args.indexOf('--control-mode') + 1], 'ai');
  assert.equal(args[args.indexOf('--speed-factor') + 1], '10');
  assert.equal(args[args.indexOf('--water-l') + 1], '12');
});

test('runs one child at a time and stops it cleanly', async () => {
  const children = [];
  const spawnImpl = () => {
    const child = fakeChild();
    children.push(child);
    setTimeout(() => child.emit('spawn'), 0);
    return child;
  };
  const job = createSimJob({ dbPath: '/tmp/db.sqlite', cwd: '/tmp', spawnImpl });

  const first = await job.start({ material: 'saltwater', water_l: 10, solute_g: 500, control_mode: 'rule' });
  assert.equal(first.ok, true);
  assert.match(first.batch_id, /^sim-saltwater-\d{4}-\d{6}-[0-9a-f]{4}$/);
  assert.equal(job.status().running, true);
  assert.equal(job.status().config.control_mode, 'rule');

  const second = await job.start({ material: 'caco3', water_l: 8, solute_g: 300, control_mode: 'ai' });
  assert.equal(second.ok, true);
  assert.deepEqual(children[0].signals, ['SIGINT']);   // 이전 실험은 먼저 종료
  assert.equal(job.status().batch_id, second.batch_id);

  const stopped = await job.stop();
  assert.equal(stopped.stopped, true);
  assert.equal(job.status().running, false);
  assert.deepEqual(children[1].signals, ['SIGINT']);
});

test('refuses invalid input without spawning', async () => {
  let spawned = 0;
  const job = createSimJob({ dbPath: '/tmp/db.sqlite', cwd: '/tmp', spawnImpl: () => { spawned += 1; return fakeChild(); } });
  const result = await job.start({ material: 'unknown' });
  assert.equal(result.ok, false);
  assert.equal(result.error, 'invalid');
  assert.equal(spawned, 0);
});

test('reports python_unavailable when the interpreter is missing', async () => {
  const spawnImpl = () => {
    const child = fakeChild();
    child.kill = () => true;
    setTimeout(() => child.emit('error', Object.assign(new Error('not found'), { code: 'ENOENT' })), 0);
    return child;
  };
  const job = createSimJob({ dbPath: '/tmp/db.sqlite', cwd: '/tmp', spawnImpl });
  const result = await job.start({ material: 'saltwater' });
  assert.equal(result.ok, false);
  assert.equal(result.error, 'python_unavailable');
  assert.equal(job.status().running, false);
});
