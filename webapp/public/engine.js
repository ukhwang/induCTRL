/**
 * 브라우저 실행용 시뮬레이션 엔진.
 *
 * inductrl_stirctl/inductrl/sim.py · mock.py · decision.py 와 같은 물리식과 판정 규칙을
 * 옮긴 판입니다. Python(자식 프로세스·SQLite)을 쓸 수 없는 정적 호스팅(Vercel)에서
 * 화면이 그대로 실험을 돌리기 위한 폴백입니다.
 *
 *  혼합  dM/dt = rate(rpm)·(1-M)/τ,  rate = 0.9·(rpm/Njs)^1.4  (rpm ≥ 0.75·Njs)
 *  동력  P = 120 W ·(V/10)^0.67 ·(1+0.35(Lf-1))·(rpm/300)^2.85 ·(1+0.22(1-M))
 *  판정  최소 교반 30 s → 10 s 창 범위/구간 조건 → 3 s 유지 → 후보 → 정지 확인(RPM 2 s 유지)
 */

export const INTERVAL_S = 0.5;
export const WINDOW_S = 10;
export const MIN_STIR_S = 30;
export const HOLD_S = 3;
export const MIN_COVERAGE = 0.9;
export const RUNNING_RPM_MIN = 100;
export const RUNNING_CONFIRM_S = 2;
export const STALE_S = 2;
export const MOTOR_START_S = 1;
export const RAMP_S = 2;
export const STOP_DECAY_S = 2;
export const STOP_HOLD_S = 2;
export const STOP_TIMEOUT_S = 10;
export const STOPPED_RPM_MAX = 5;

export const FIXED_RPM = 400;
export const FIXED_SECONDS = 120;
export const RULE_RPM = 320;
export const AI_DIAG_RPM = 260;
export const AI_BURST_RATIO = 1.5;
export const AI_CRUISE_RATIO = 1.15;
export const AI_BLEND_S = 1.5;

export const NJS_BASE_RPM = 190;
export const REF_CONCENTRATION = 50;
export const REF_VOLUME_L = 10;
export const TAU_MIX_S = 14;
export const P_REF_W = 120;
export const SUSPEND_RATIO = 0.75;
export const TAU_SETTLE_S = 25;

export const CHANNELS = {
  saltwater: ['conductivity'],
  caco3: ['turbidity_top', 'turbidity_bottom'],
};
const ALL_ANALOG = ['conductivity', 'turbidity_top', 'turbidity_bottom'];
const CV_SCALE = { saltwater: 12, caco3: 18 };
const BANDS = {
  saltwater: { conductivity_min: 45, conductivity_max: 55, conductivity_max_range: 1 },
  caco3: {
    turbidity_top_min: 400, turbidity_top_max: 600,
    turbidity_bottom_min: 400, turbidity_bottom_max: 600,
    turbidity_max_range: 20, top_bottom_max_diff: 15,
  },
};
const EPS = 1e-6;

const range = (value, lo, hi) => (Number.isFinite(value) ? Math.min(hi, Math.max(lo, value)) : null);

/** 입력을 검증합니다. 화면·서버와 같은 규칙(0.5~200 L, 0~20000 g, 0.25~50x). */
export function normalizeConfig(input = {}) {
  const material = ['saltwater', 'caco3'].includes(input.material) ? input.material : null;
  const control = ['fixed', 'rule', 'ai'].includes(input.control_mode) ? input.control_mode : null;
  const water = range(Number(input.water_l), 0.5, 200);
  const solute = range(Number(input.solute_g), 0, 20000);
  const speed = range(Number(input.speed_factor ?? 5), 0.25, 50);
  if (!material || !control || water === null || solute === null || speed === null) return null;
  return { material, control_mode: control, water_l: water, solute_g: solute, speed_factor: speed };
}

const mulberry32 = (seed) => () => {
  seed |= 0;
  seed = (seed + 0x6d2b79f5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};

export const powerW = (rpm, waterL, load, mix) => {
  if (rpm <= 0) return 0;
  const volume = (waterL / REF_VOLUME_L) ** 0.67;
  const loadTerm = 1 + 0.35 * (load - 1);
  return P_REF_W * volume * loadTerm * (rpm / 300) ** 2.85 * (1 + 0.22 * (1 - mix));
};

export const mixRate = (rpm, njs) => {
  if (njs <= 0 || rpm < SUSPEND_RATIO * njs) return 0;
  return Math.min(3, 0.9 * (rpm / njs) ** 1.4);
};

function stepMix(mix, rpm, params, dt) {
  const rate = mixRate(rpm, params.njs);
  const next = rate > 0 ? mix + rate * (1 - mix) * dt / params.tau : mix - mix * dt / TAU_SETTLE_S;
  return Math.min(1, Math.max(0, next));
}

/** FIXED 기준 운전(고정 회전·고정 시간)의 모델 계산 에너지[Wh]. */
export function fixedBaselineEnergyWh(params, step = 0.5) {
  const steps = Math.max(1, Math.round(FIXED_SECONDS / step));
  let mix = 0;
  let energy = 0;
  for (let index = 0; index < steps; index += 1) {
    const t = index * step;
    const target = t >= MOTOR_START_S ? FIXED_RPM : 0;
    const rpm = t >= MOTOR_START_S ? Math.min(target, target * (t - MOTOR_START_S) / RAMP_S) : 0;
    mix = stepMix(mix, rpm, params, step);
    energy += powerW(rpm, params.water_l, params.load, mix) * step / 3600;
  }
  return energy;
}

/** 한 번의 실험. step() 을 0.5초(설비 시간)씩 부르면 stirctl.py mock 과 같은 순서로 진행됩니다. */
export class SimEngine {
  constructor(config, { seed = 7 } = {}) {
    this.config = normalizeConfig(config);
    if (!this.config) throw new Error('invalid config');
    const { material, water_l, solute_g, control_mode } = this.config;
    this.material = material;
    this.mode = control_mode;
    this.waterL = water_l;
    this.soluteG = solute_g;
    this.concentration = solute_g / water_l;
    this.load = Math.min(2.5, Math.max(0.25, (this.concentration / REF_CONCENTRATION) ** 0.55));
    this.njs = NJS_BASE_RPM * this.load;
    this.tau = TAU_MIX_S * (water_l / REF_VOLUME_L) ** 0.35 * Math.sqrt(this.load);
    this.rand = mulberry32(seed * 7919 + Math.round(water_l * 13 + solute_g));
    this.channels = CHANNELS[material];
    this.baselineEnergyWh = fixedBaselineEnergyWh(this.params);

    this.t = 0;
    this.mix = 0;
    this.energy = 0;
    this.rpm = 0;
    this.power = 0;
    this.cv = CV_SCALE[material];
    this.samples = [];
    this.history = [];
    this.readings = {};
    this.lowSince = null;
    this.events = [];
    this.stopRequested = false;
    this.stopAt = null;
    this.rpmAtStop = 0;
    this.stopConfirmedAt = null;
    this.decision = {
      t0: null, runningSince: null, holdStart: null, candidateT: null,
      state: 'WAITING_START', reasons: ['waiting_start'], hold: null, confirmed: false,
    };
    this.window = Object.fromEntries(ALL_ANALOG.map((ch) => [ch, []]));
    this.finished = false;
    this.finalStatus = 'RUNNING';
    this.stalled = false;
    this.latest = {};
    this.pushEvent('run_started', '수집 시작');
    this.capture(0);
  }

  /* ── 회전수 정책 ───────────────────────────────────────────────────── */
  commandRpm(tau = this.t) {
    if (this.mode === 'fixed') return FIXED_RPM;
    if (this.mode === 'rule') return RULE_RPM;
    const steps = [[0, AI_DIAG_RPM], [6, Math.min(420, AI_BURST_RATIO * this.njs)],
      [14, Math.max(120, AI_CRUISE_RATIO * this.njs)]];
    let value = steps[0][1];
    for (let index = 1; index < steps.length; index += 1) {
      const [start, level] = steps[index];
      if (tau >= start) {
        const blend = Math.min(1, (tau - start) / AI_BLEND_S);
        value = steps[index - 1][1] + (level - steps[index - 1][1]) * blend;
      }
    }
    return value;
  }

  phase() {
    if (this.stalled) return 'STALLED';
    if (this.mode === 'fixed') return 'FIXED';
    if (this.mode === 'rule') return 'HOLD';
    if (this.t < 6) return 'DIAGNOSE';
    if (this.t < 14) return 'SUSPEND';
    return 'CRUISE';
  }

  rpmAt(t) {
    const target = this.commandRpm(t);
    let base = 0;
    if (t >= MOTOR_START_S) base = Math.min(target, target * (t - MOTOR_START_S) / RAMP_S);
    if (this.stopAt !== null && t >= this.stopAt) {
      return Math.max(0, this.rpmAtStop * (1 - (t - this.stopAt) / STOP_DECAY_S));
    }
    return base;
  }

  stop(t = this.t) {
    if (this.stopAt === null) {
      this.rpmAtStop = this.rpmAt(t);
      this.stopAt = t;
      this.pushEvent('stop_requested', '정지 요청');
    }
  }

  /* ── 센서 ──────────────────────────────────────────────────────────── */
  noise(rpm, amp) {
    const scale = 0.5 + 0.5 * Math.min(1, Math.max(0, rpm / 300));
    return (this.rand() * 2 - 1) * amp * scale;
  }

  sensor(ch, rpm = this.rpm, unmixed = 1 - this.mix) {
    if (ch === 'conductivity') {
      if (this.material === 'saltwater') return 50 + 6 * unmixed + this.noise(rpm, 0.06);
      return 7 + 2 * unmixed + this.noise(rpm, 0.05);
    }
    if (this.material === 'caco3') {
      return 500 + (ch === 'turbidity_top' ? -170 : 170) * unmixed + this.noise(rpm, 0.8);
    }
    return 45 + (ch === 'turbidity_top' ? -8 : 8) * unmixed + this.noise(rpm, 0.4);
  }

  /* ── 판정(decision.py 이식) ───────────────────────────────────────── */
  thresholdFailures(values) {
    const th = BANDS[this.material];
    const failed = [];
    const check = (ch, vals, loKey, hiKey, rangeKey) => {
      const lo = th[loKey];
      const hi = th[hiKey];
      const span = th[rangeKey];
      if (vals.length === 0) return;
      if (lo !== undefined && Math.min(...vals) < lo) failed.push(`below_min:${ch}`);
      if (hi !== undefined && Math.max(...vals) > hi) failed.push(`above_max:${ch}`);
      if (span !== undefined && Math.max(...vals) - Math.min(...vals) > span + 1e-12) failed.push(`range:${ch}`);
    };
    if (this.material === 'saltwater') {
      check('conductivity', values.conductivity ?? [], 'conductivity_min', 'conductivity_max', 'conductivity_max_range');
    } else {
      const top = values.turbidity_top ?? [];
      const bot = values.turbidity_bottom ?? [];
      check('turbidity_top', top, 'turbidity_top_min', 'turbidity_top_max', 'turbidity_max_range');
      check('turbidity_bottom', bot, 'turbidity_bottom_min', 'turbidity_bottom_max', 'turbidity_max_range');
      const diff = th.top_bottom_max_diff;
      if (top.length && bot.length && diff !== undefined) {
        const mean = (list) => list.reduce((sum, value) => sum + value, 0) / list.length;
        if (Math.abs(mean(top) - mean(bot)) > diff + 1e-12) failed.push('top_bottom_diff');
      }
    }
    return failed;
  }

  evaluate(t, rpm, values) {
    const reasons = [];
    if (t - this.decision.t0 + EPS < MIN_STIR_S) reasons.push('min_stir');
    if (rpm === null) reasons.push('rpm_invalid');
    else if (rpm < RUNNING_RPM_MIN) reasons.push('not_running');
    if (t - WINDOW_S + EPS < this.decision.t0) reasons.push('window_not_filled');
    const required = WINDOW_S / INTERVAL_S * MIN_COVERAGE;
    for (const ch of this.channels) {
      const vals = (values[ch] ?? []).filter((value) => value !== null);
      if (vals.length + EPS < required) reasons.push(`coverage:${ch}(${vals.length}/${required.toFixed(1)})`);
    }
    reasons.push(...this.thresholdFailures(values));
    return reasons;
  }

  updateDecision(t, rpm, values) {
    const d = this.decision;
    if (d.t0 === null) {
      if (rpm !== null && rpm >= RUNNING_RPM_MIN) {
        d.runningSince ??= t;
        if (t - d.runningSince + EPS >= RUNNING_CONFIRM_S) {
          d.t0 = t;
          this.pushEvent('batch_started', '교반 시작');
        }
      } else {
        d.runningSince = null;
      }
      if (d.t0 === null) {
        d.state = 'WAITING_START';
        d.reasons = ['waiting_start'];
        d.hold = null;
        return;
      }
    }
    const reasons = this.evaluate(t, rpm, values);
    const met = reasons.length === 0;
    if (met) {
      d.holdStart ??= t;
      d.hold = t - d.holdStart;
      if (d.candidateT !== null) {
        d.state = 'MET_AFTER_CANDIDATE';
      } else if (d.hold + EPS >= HOLD_S) {
        d.candidateT = t;
        d.state = 'CANDIDATE';
        this.pushEvent('candidate', '교반 완료 후보');
        if (this.mode !== 'fixed' && !this.stopRequested) {
          this.stopRequested = true;
          this.stop(t);
        }
      } else {
        d.state = 'HOLDING';
      }
    } else {
      d.holdStart = null;
      d.hold = null;
      d.state = reasons.some((reason) => reason.startsWith('min_stir')) ? 'MIN_STIR' : 'NOT_MET';
      if (d.candidateT !== null) d.state = 'NOT_MET';
    }
    d.reasons = reasons;
  }

  /* ── 한 주기 진행 ──────────────────────────────────────────────────── */
  step() {
    if (this.finished) return this;
    this.t = Math.round((this.t + INTERVAL_S) * 1e6) / 1e6;
    const t = this.t;
    const rpmMid = this.rpmAt(t - INTERVAL_S / 2);
    if (!this.stopAt && t > 8 && rpmMid > 0.3 * this.njs && mixRate(rpmMid, this.njs) <= 0) {
      this.stalled = true;
      if (!this.events.some((event) => event.code === 'warning')) {
        this.pushEvent('warning', '회전수가 부상 임계 미만 — 침전물이 뜨지 않음');
      }
    }
    this.mix = stepMix(this.mix, rpmMid, this.params, INTERVAL_S);
    this.energy += powerW(rpmMid, this.waterL, this.load, this.mix) * INTERVAL_S / 3600;
    this.rpm = this.rpmAt(t);
    this.power = powerW(this.rpm, this.waterL, this.load, this.mix);
    this.cv = CV_SCALE[this.material] * (1 - this.mix);

    const values = {};
    const readings = {};
    for (const ch of ALL_ANALOG) {
      const value = this.sensor(ch);
      readings[ch] = value;
      this.window[ch].push({ t, value });
      while (this.window[ch].length && this.window[ch][0].t < t - WINDOW_S - 5) this.window[ch].shift();
      if (this.channels.includes(ch)) {
        values[ch] = this.window[ch]
          .filter((point) => t - WINDOW_S + EPS < point.t && point.t <= t + EPS)
          .map((point) => point.value);
      }
    }
    this.readings = readings;
    this.updateDecision(t, this.rpm, values);
    this.checkStop(t);
    this.checkFinish(t);
    this.capture(t);
    return this;
  }

  get params() {
    return { water_l: this.waterL, load: this.load, njs: this.njs, tau: this.tau };
  }

  checkStop(t) {
    const d = this.decision;
    if (d.confirmed || this.stopAt === null) return;
    if (this.rpm <= STOPPED_RPM_MAX + EPS) {
      this.lowSince ??= t;
      if (t - this.lowSince + EPS >= STOP_HOLD_S) {
        d.confirmed = true;
        d.state = 'STOPPED';
        this.stopConfirmedAt = t;
        this.pushEvent('stop_confirmed', '정지 확인');
      }
    } else {
      this.lowSince = null;
    }
  }

  checkFinish(t) {
    if (this.finished) return;
    if (this.mode === 'fixed') {
      if (t >= FIXED_SECONDS - 1e-6) {
        this.finish(this.decision.candidateT !== null ? 'CANDIDATE_UNVERIFIED' : 'NO_CANDIDATE');
      }
      return;
    }
    if (this.stopConfirmedAt !== null && t - this.stopConfirmedAt >= 6) {
      this.finish('AUTO_STOP_CONFIRMED');
      return;
    }
    if (t >= 180) this.finish(this.decision.candidateT !== null ? 'CANDIDATE_UNVERIFIED' : 'NO_CANDIDATE');
  }

  finish(status) {
    this.finished = true;
    this.finalStatus = status;
    if (status === 'AUTO_STOP_CONFIRMED' && this.decision.state !== 'STOPPED') this.decision.state = 'STOPPED';
    this.pushEvent('run_finished', '수집 종료');
  }

  pushEvent(code, title) {
    this.events.push({ code, title, elapsed_s: this.t });
    if (this.events.length > 60) this.events.shift();
  }

  capture(t) {
    const point = {
      elapsed_s: t, rpm: this.rpm, rpm_setpoint: this.commandRpm(t), phase: this.phase(),
      power_w: this.power, energy_wh: this.energy, cv_pct: this.cv, mix: this.mix,
    };
    const last = this.history.at(-1);
    if (!last || Math.abs(last.elapsed_s - t) > 1e-9) {
      this.history.push(point);
      const readings = this.readings ?? {};
      this.samples.push({
        elapsed_s: t, rpm: this.rpm,
        conductivity: readings.conductivity ?? null,
        turbidity_top: readings.turbidity_top ?? null,
        turbidity_bottom: readings.turbidity_bottom ?? null,
      });
    }
    this.latest = point;
  }

  /** 화면이 쓰는 현재 상태(서버 응답과 같은 모양). */
  snapshot() {
    const judged = this.decision.candidateT ?? this.stopConfirmedAt ?? this.t;
    const result = this.history.reduce((best, point) =>
      (Math.abs(point.elapsed_s - judged) < Math.abs(best.elapsed_s - judged) ? point : best), this.history[0]);
    return {
      mode: 'local',
      material: this.material,
      control_mode: this.mode,
      water_l: this.waterL,
      solute_g: this.soluteG,
      concentration_g_per_l: this.concentration,
      njs_rpm: this.njs,
      baseline_energy_wh: this.baselineEnergyWh,
      elapsed_s: this.t,
      running: !this.finished,
      finished: this.finished,
      final_status: this.finalStatus,
      decision: { ...this.decision },
      state: this.decision.state,
      phase: this.phase(),
      rpm: this.rpm,
      power_w: this.power,
      energy_wh: this.energy,
      cv_pct: this.cv,
      mix: this.mix,
      latest: this.latest,
      result,
      total: this.history.at(-1),
      history: this.history,
      samples: this.samples,
      events: this.events,
    };
  }

  /** 테스트용: 끝날 때까지 한 번에 돌립니다. */
  runAll(limit = 400) {
    for (let index = 0; index < limit && !this.finished; index += 1) this.step();
    return this;
  }
}

export default SimEngine;
