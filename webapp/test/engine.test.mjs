import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { SimEngine, fixedBaselineEnergyWh, normalizeConfig } from '../public/engine.js';

const run = (config) => {
  const engine = new SimEngine(config);
  engine.runAll();
  return engine.snapshot();
};

test('validates experiment input like the server does', () => {
  assert.deepEqual(normalizeConfig({ material: 'saltwater', control_mode: 'ai', water_l: 10, solute_g: 500 }),
    { material: 'saltwater', control_mode: 'ai', water_l: 10, solute_g: 500, speed_factor: 5 });
  assert.equal(normalizeConfig({ material: 'saltwater', control_mode: 'auto' }), null);
  assert.equal(normalizeConfig({ material: 'caco3', water_l: -1 }), null);
});

test('rule mode stops on the stability decision (30s min stir + 3s hold)', () => {
  const state = run({ material: 'saltwater', control_mode: 'rule', water_l: 10, solute_g: 500, speed_factor: 5 });
  assert.equal(state.final_status, 'AUTO_STOP_CONFIRMED');
  assert.ok(state.decision.candidateT >= 30, `candidate at ${state.decision.candidateT}`);
  assert.ok(state.decision.candidateT <= 45);
  assert.ok(state.energy_wh > 1 && state.energy_wh < 3, `energy ${state.energy_wh}`);
  const codes = state.events.map((event) => event.code);
  assert.deepEqual(codes, ['run_started', 'batch_started', 'candidate', 'stop_requested', 'stop_confirmed',
    'run_finished']);
});

test('ai mode finishes with less energy than rule at a similar finish time', () => {
  const rule = run({ material: 'saltwater', control_mode: 'rule', water_l: 10, solute_g: 500, speed_factor: 5 });
  const ai = run({ material: 'saltwater', control_mode: 'ai', water_l: 10, solute_g: 500, speed_factor: 5 });
  assert.equal(ai.final_status, 'AUTO_STOP_CONFIRMED');
  assert.ok(ai.energy_wh < rule.energy_wh * 0.75, `ai ${ai.energy_wh} rule ${rule.energy_wh}`);
  assert.ok(Math.abs(ai.decision.candidateT - rule.decision.candidateT) <= 6);
  assert.equal(ai.phase, 'CRUISE');
});

test('fixed mode keeps running the full comparison window without stopping', () => {
  const state = run({ material: 'saltwater', control_mode: 'fixed', water_l: 10, solute_g: 500, speed_factor: 10 });
  assert.equal(state.final_status, 'CANDIDATE_UNVERIFIED');
  assert.equal(state.elapsed_s, 120);
  assert.ok(!state.events.some((event) => event.code === 'stop_requested'));
  assert.ok(Math.abs(state.energy_wh - fixedBaselineEnergyWh({
    water_l: 10, load: 1, njs: 190, tau: 14 })) < 0.4);
});

test('a heavier charge needs more time and energy', () => {
  const light = run({ material: 'caco3', control_mode: 'ai', water_l: 20, solute_g: 200, speed_factor: 5 });
  const heavy = run({ material: 'caco3', control_mode: 'ai', water_l: 5, solute_g: 1500, speed_factor: 5 });
  assert.equal(heavy.final_status, 'AUTO_STOP_CONFIRMED');
  assert.ok(heavy.decision.candidateT > light.decision.candidateT);
  assert.ok(heavy.energy_wh > light.energy_wh);
});

test('fixed speed below the suspension threshold is reported, not silently mixed', () => {
  const state = run({ material: 'saltwater', control_mode: 'rule', water_l: 5, solute_g: 1500, speed_factor: 5 });
  assert.equal(state.final_status, 'NO_CANDIDATE');
  assert.equal(state.phase, 'STALLED');
  assert.ok(state.events.some((event) => event.code === 'warning'));
});

test('browser engine matches the Python stirctl result for the same charge', (t) => {
  const root = fileURLToPath(new URL('../../inductrl_stirctl/', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'inductrl-engine-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, 'compare.sqlite');
  try {
    execFileSync('python3', ['stirctl.py', 'run', '--mode', 'mock', '--material', 'saltwater',
      '--batch-id', 'cmp', '--db', path, '--control-mode', 'ai', '--water-l', '10', '--solute-g', '500',
      '--fast', '--status-every', '0'], { cwd: root, stdio: 'ignore' });
  } catch (error) {
    t.skip(`python3 실행 불가: ${error.code ?? error.message}`);
    return;
  }
  const db = new DatabaseSync(path, { readOnly: true });
  const rows = db.prepare('SELECT elapsed_s, energy_wh, cv_pct FROM sim_metrics WHERE batch_id = ? ORDER BY seq')
    .all('cmp');
  const stop = db.prepare("SELECT elapsed_s FROM events WHERE batch_id = ? AND event_type = 'candidate'")
    .get('cmp');
  db.close();
  const python = { elapsed_s: stop.elapsed_s, energy_wh: rows.at(-1).energy_wh };
  const js = run({ material: 'saltwater', control_mode: 'ai', water_l: 10, solute_g: 500, speed_factor: 5 });
  assert.ok(Math.abs(js.decision.candidateT - python.elapsed_s) <= 6,
    `candidate js=${js.decision.candidateT} py=${python.elapsed_s}`);
  assert.ok(Math.abs(js.total.energy_wh - python.energy_wh) / python.energy_wh < 0.15,
    `energy js=${js.total.energy_wh} py=${python.energy_wh}`);
});
