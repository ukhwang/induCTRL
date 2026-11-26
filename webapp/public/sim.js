/**
 * 실시간 실험 시뮬레이터 화면.
 *
 * 실행 경로 두 가지
 *  1) 서버 모드: /api/sim/start → stirctl.py(mock) + SQLite (로컬·MIC 환경)
 *  2) 브라우저 모드: 같은 물리식·판정 규칙을 engine.js 로 실행 (정적 호스팅·Vercel,
 *     또는 python 이 없는 환경에서 자동 폴백)
 * 두 경로 모두 같은 view 객체를 만들어 같은 화면(탱크 애니메이션·계측·결과 카드)을 그립니다.
 */
import { SimEngine, normalizeConfig, INTERVAL_S } from './engine.js';
import { drawChart } from './chart.js';
import { mountSegments, paintSegments, prefersReducedMotion, tweenNumber } from './ui.js';

const root = document.querySelector('#sim');

const LABELS = {
  saltwater: '소금물 실험 (김치 염수)',
  caco3: '탄산칼슘 실험 (막걸리 침전물)',
};
const MODE_LABELS = { fixed: 'FIXED', rule: 'RULE', ai: 'AI' };
const STATUS_TEXT = {
  WAITING_START: '교반 시작 대기',
  BLOCKED: '판정 차단',
  MIN_STIR: '최소 교반 30초',
  NOT_MET: '균일화 진행 중',
  HOLDING: '안정 조건 유지 중',
  CANDIDATE: '교반 완료 후보',
  MET_AFTER_CANDIDATE: '완료 후보 이후',
  STOPPED: '정지 확인',
};

/** 계측 타일 정의. 화면을 한 번만 만들고 숫자만 부드럽게 갱신합니다. */
const TILE_DEFS = [
  { key: 'rpm', label: '회전수', unit: 'RPM', digits: 0 },
  { key: 'power_w', label: '축동력', unit: 'W', digits: 1 },
  { key: 'energy_wh', label: '누적 에너지', unit: 'Wh', digits: 3 },
  { key: 'cv_pct', label: '잔여 불균일', unit: '%', digits: 2 },
];

function init() {
  const el = {
    badge: root.querySelector('#sim-badge'),
    canvas: root.querySelector('#sim-canvas'),
    materialLabel: root.querySelector('#sim-material-label'),
    elapsed: root.querySelector('#sim-elapsed'),
    form: root.querySelector('#sim-form'),
    water: root.querySelector('#water'),
    solute: root.querySelector('#solute'),
    waterOut: root.querySelector('#water-out'),
    soluteOut: root.querySelector('#solute-out'),
    modeSeg: root.querySelector('#mode-seg'),
    speedSeg: root.querySelector('#speed-seg'),
    start: root.querySelector('#sim-start'),
    stop: root.querySelector('#sim-stop'),
    note: root.querySelector('#sim-note'),
    tiles: root.querySelector('#sim-tiles'),
    chartSensor: root.querySelector('#sim-chart-sensor'),
    chartPower: root.querySelector('#sim-chart-power'),
    chartSensorLabel: root.querySelector('#sim-chart-sensor-label'),
    chartPowerLabel: root.querySelector('#sim-chart-power-label'),
    events: root.querySelector('#sim-events'),
    result: root.querySelector('#sim-result'),
    mix: root.querySelector('#sim-mix'),
    mixFill: root.querySelector('#sim-mix-fill'),
    mixOut: root.querySelector('#sim-mix-out'),
  };

  const state = {
    mode: null,          // 'server' | 'local'
    config: { material: 'saltwater', control_mode: 'rule', water_l: 10, solute_g: 500, speed_factor: 5 },
    engine: null,
    batchId: null,
    view: null,
    stepTimer: null,
    pollTimer: null,
    rafTimer: null,
    busy: false,
    serverUnavailable: false,
  };

  /* ── 입력 ─────────────────────────────────────────────────────────── */
  const readForm = () => ({
    material: el.form.querySelector('input[name="material"]:checked')?.value ?? 'saltwater',
    water_l: Number(el.water.value),
    solute_g: Number(el.solute.value),
    control_mode: [...el.modeSeg.querySelectorAll('button')].find((b) => b.getAttribute('aria-pressed') === 'true')?.dataset.mode
      ?? 'rule',
    speed_factor: Number([...el.speedSeg.querySelectorAll('button')].find((b) => b.getAttribute('aria-pressed') === 'true')?.dataset.speed
      ?? 5),
  });

  const syncOutputs = () => {
    el.waterOut.textContent = `${el.water.value} L`;
    el.soluteOut.textContent = `${el.solute.value} g`;
    const conc = (Number(el.solute.value) / Number(el.water.value)).toFixed(0);
    const hint = root.querySelector('#sim-concentration');
    if (hint) hint.textContent = `농도 ${conc} g/L`;
    el.materialLabel.textContent = LABELS[readForm().material];
  };

  el.form.addEventListener('input', (event) => {
    if (event.target === el.water || event.target === el.solute) syncOutputs();
  });
  el.form.addEventListener('change', syncOutputs);
  el.form.addEventListener('change', (event) => {
    if (event.target.name === 'material') paintTankStatic();
  });
  for (const group of [el.modeSeg, el.speedSeg]) {
    group.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button || button.disabled) return;
      for (const other of group.querySelectorAll('button')) other.setAttribute('aria-pressed', 'false');
      button.setAttribute('aria-pressed', 'true');
      syncOutputs();
    });
  }
  el.form.addEventListener('submit', (event) => {
    event.preventDefault();
    startExperiment();
  });
  el.stop.addEventListener('click', () => stopExperiment(true));

  /* ── 실행 ─────────────────────────────────────────────────────────── */
  async function startExperiment() {
    if (state.busy) return;
    const config = readForm();
    const restarting = !el.result.hidden;
    state.config = config;
    state.busy = true;
    resetRun();
    setBadge('준비 중', 'pending');
    el.start.disabled = true;
    el.start.setAttribute('aria-busy', 'true');
    el.start.textContent = '준비 중…';
    el.note.textContent = '실험을 준비하고 있습니다.';
    let started = false;
    try {
      const response = await fetch('/api/sim/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
        signal: AbortSignal.timeout(5000),
      });
      const payload = await response.json().catch(() => null);
      if (response.ok && payload?.ok) {
        state.mode = 'server';
        state.batchId = payload.batch_id;
        state.serverUnavailable = false;
        started = true;
        el.note.textContent = `서버 시뮬레이터(MIC 모의) 실행 · ${payload.batch_id}`;
        pollServer();
      } else if (payload?.error === 'python_unavailable' || payload?.error === 'sim_disabled') {
        state.serverUnavailable = true;
      }
    } catch {
      state.serverUnavailable = true;
    }
    if (!started) {
      startLocal(state.serverUnavailable
        ? '브라우저 엔진으로 실행합니다 — 이 주소에는 Python 실행 환경이 없습니다.'
        : '브라우저 엔진으로 실행합니다.');
    }
    el.stop.disabled = false;
    state.busy = false;
    el.start.removeAttribute('aria-busy');
    el.start.textContent = '실험 시작';
    if (restarting) root.scrollIntoView({ block: 'start', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  }

  function startLocal(note) {
    state.mode = 'local';
    state.batchId = `local-${Date.now().toString(36)}`;
    state.engine = new SimEngine(state.config, { seed: 7 });
    const stepMs = Math.max(40, (INTERVAL_S / state.config.speed_factor) * 1000);
    state.stepTimer = setInterval(() => {
      if (!state.engine) return;
      state.engine.step();
      present(localView(state.engine));
      if (state.engine.finished) {
        clearInterval(state.stepTimer);
        state.stepTimer = null;
        finishRun();
      }
    }, stepMs);
    el.note.textContent = note;
    present(localView(state.engine));
  }

  function stopExperiment(manual) {
    if (state.mode === 'server') {
      fetch('/api/sim/stop', { method: 'POST' }).catch(() => {});
      clearTimeout(state.pollTimer);
    } else if (state.stepTimer) {
      clearInterval(state.stepTimer);
      state.stepTimer = null;
    }
    if (manual && state.view && !state.view.finished) {
      el.note.textContent = '실험을 중단했습니다.';
      setBadge('중단', 'idle');
      el.start.disabled = false;
      el.stop.disabled = true;
      if (state.engine) {
        state.engine.finished = true;
        state.engine.finalStatus = 'INTERRUPTED';
      }
      finishRun(true);
    }
    state.mode = null;
  }

  async function pollServer() {
    try {
      const statusResponse = await fetch('/api/sim/status', { cache: 'no-store', signal: AbortSignal.timeout(4000) });
      const status = await statusResponse.json();
      // 실행 직후에는 배치 행이 아직 없습니다. 그때 /api/batch 를 부르면 404 가 콘솔에 남습니다.
      let batch = null;
      if (status?.batch) {
        const batchResponse = await fetch(`/api/batch?id=${encodeURIComponent(state.batchId)}`,
          { cache: 'no-store', signal: AbortSignal.timeout(4000) });
        if (batchResponse.ok) batch = await batchResponse.json();
      }
      present(serverView(status, batch));
      if (status.running) {
        state.pollTimer = setTimeout(pollServer, 500);
      } else {
        finishRun();
      }
    } catch {
      el.note.textContent = '서버 상태를 읽지 못했습니다. 잠시 후 다시 시도합니다.';
      state.pollTimer = setTimeout(pollServer, 1500);
    }
  }

  /* ── 뷰 다듬기 ────────────────────────────────────────────────────── */
  function localView(engine) {
    const snap = engine.snapshot();
    return {
      source: 'local',
      running: !snap.finished,
      finished: snap.finished,
      final_status: snap.final_status,
      state: snap.state,
      phase: snap.phase,
      elapsed_s: snap.elapsed_s,
      rpm: snap.rpm,
      power_w: snap.power_w,
      energy_wh: snap.energy_wh,
      cv_pct: snap.cv_pct,
      mix: snap.mix,
      result: snap.result,
      total: snap.total,
      history: snap.history,
      samples: snap.samples,
      events: snap.events,
      water_l: snap.water_l,
      solute_g: snap.solute_g,
      concentration: snap.concentration_g_per_l,
      njs_rpm: snap.njs_rpm,
      baseline: snap.baseline_energy_wh,
      control_mode: snap.control_mode,
      material: snap.material,
    };
  }

  function serverView(status, batch) {
    const sim = batch?.sim ?? status?.batch?.sim ?? null;
    const history = sim?.history ?? [];
    const last = sim?.latest ?? null;
    const finished = !status.running;
    const decisionElapsed = sim?.decided_at_s ?? history.at(-1)?.elapsed_s ?? 0;
    return {
      source: 'server',
      running: Boolean(status.running),
      final_status: finished ? (batch?.final_status ?? status?.batch?.final_status ?? 'RUNNING') : (batch?.final_status ?? 'RUNNING'),
      state: batch?.current_decision?.state ?? (last ? 'HOLDING' : 'WAITING_START'),
      phase: last?.phase ?? 'IDLE',
      elapsed_s: batch?.elapsed_s ?? history.at(-1)?.elapsed_s ?? 0,
      rpm: last?.rpm ?? null,
      power_w: last?.power_w ?? null,
      energy_wh: last?.energy_wh ?? null,
      cv_pct: last?.cv_pct ?? null,
      mix: last?.mix ?? null,
      result: sim?.result ?? null,
      total: sim?.total ?? last,
      history,
      samples: batch?.samples ?? [],
      events: (batch?.events ?? []).map((event) => ({ code: event.code, title: event.title, elapsed_s: event.elapsed_s })),
      water_l: sim?.water_l ?? state.config.water_l,
      solute_g: sim?.solute_g ?? state.config.solute_g,
      concentration: sim?.concentration_g_per_l ?? null,
      njs_rpm: sim?.njs_rpm ?? null,
      baseline: sim?.baseline_energy_wh ?? null,
      control_mode: sim?.control_mode ?? state.config.control_mode,
      material: batch?.material ?? state.config.material,
      decisionElapsed,
      finished,
    };
  }

  /* ── 그리기: 계측·차트·결과 ─────────────────────────────────────────── */
  const tileNodes = new Map();
  let tilesBuilt = false;
  let eventSignature = '';
  let chartSignature = '';
  let chartFrame = 0;

  function buildTiles() {
    el.tiles.innerHTML = TILE_DEFS.map((def) =>
      `<div class="sim-tile"><span class="label">${def.label}</span><b data-tile="${def.key}">—</b><span class="unit">${def.unit}</span></div>`).join('');
    tileNodes.clear();
    for (const node of el.tiles.querySelectorAll('[data-tile]')) tileNodes.set(node.dataset.tile, node);
    tilesBuilt = true;
  }

  /**
   * 실험이 끝나면 계측 타일·게이지를 '판정 시점' 값으로 고정합니다.
   * 자동 정지 뒤에는 교반이 멈춰 혼합도가 다시 떨어지므로, 그 값을 그대로 보여 주면
   * 결과 카드(0.09 %)와 타일(4 %대)이 서로 다른 말을 하게 됩니다.
   */
  function frozenView(view) {
    if (!view || !view.finished) return view;
    const point = (view.control_mode === 'fixed' ? view.total : view.result) ?? view.total;
    if (!point) return view;
    return {
      ...view,
      energy_wh: point.energy_wh ?? view.energy_wh,
      cv_pct: point.cv_pct ?? view.cv_pct,
      mix: point.mix ?? view.mix,
    };
  }

  function present(rawView) {
    const view = frozenView(rawView);
    state.view = view;
    if (!view) return;
    if (!tilesBuilt) buildTiles();
    for (const def of TILE_DEFS) {
      tweenNumber(tileNodes.get(def.key), view[def.key], { digits: def.digits });
    }
    updateMix(view);
    const decider = view.material === 'saltwater' ? 'mS/cm 전도도' : 'NTU 탁도';
    const sensorLabel = `판정 변수 · ${decider}`;
    if (el.chartSensorLabel.textContent !== sensorLabel) el.chartSensorLabel.textContent = sensorLabel;
    const elapsed = clock(view.elapsed_s ?? 0);
    if (el.elapsed.textContent !== elapsed) el.elapsed.textContent = elapsed;
    setBadge(badgeOf(view), view.finished ? 'done' : view.running ? 'live' : 'idle');
    setLocked(Boolean(view.running && !view.finished));
    renderEvents(view);
    scheduleCharts(view);
    if (view.running && !view.finished) el.start.disabled = true;
  }

  /** 혼합도 게이지. scaleX 만 움직여 레이아웃을 건드리지 않습니다. */
  function updateMix(view) {
    const mix = Math.min(1, Math.max(0, Number.isFinite(view.mix) ? view.mix : 0));
    el.mixFill.style.transform = `scaleX(${mix.toFixed(3)})`;
    const percent = Math.round(mix * 100);
    if (el.mixOut.textContent !== `${percent}%`) el.mixOut.textContent = `${percent}%`;
    if (el.mix.getAttribute('aria-valuenow') !== String(percent)) el.mix.setAttribute('aria-valuenow', String(percent));
  }

  function badgeOf(view) {
    if (view.phase === 'STALLED') return '회전수 부족';
    if (view.final_status === 'AUTO_STOP_CONFIRMED') return '정지 확인';
    if (view.final_status === 'INTERRUPTED') return '중단';
    if (!view.running && view.finished) {
      return view.final_status === 'NO_CANDIDATE' ? '완료 후보 없음' : '실험 종료';
    }
    return STATUS_TEXT[view.state] ?? '교반 중';
  }

  function setBadge(text, kind) {
    const next = kind ?? 'idle';
    if (el.badge.textContent === text && el.badge.dataset.kind === next) return;
    el.badge.textContent = text;
    el.badge.dataset.kind = next;
  }

  /** 실행 중에는 투입량·모드·속도를 잠급니다. 바꾸면 다음 실험부터 적용되기 때문입니다. */
  let locked = false;
  function setLocked(value) {
    if (locked === value) return;
    locked = value;
    for (const node of el.form.querySelectorAll('input, .segment button')) node.disabled = value;
    el.form.classList.toggle('is-locked', value);
  }

  function renderEvents(view) {
    const events = (view.events ?? []).slice(-5).reverse();
    const signature = events.map((event) => `${event.elapsed_s}:${event.code ?? event.title}`).join('|');
    if (signature === eventSignature) return;
    eventSignature = signature;
    if (!events.length) {
      el.events.innerHTML = '<li class="muted">실험을 시작하면 판정 이벤트가 쌓입니다.</li>';
      return;
    }
    el.events.innerHTML = events.map((event) => `<li><span class="dot" aria-hidden="true"></span>
      <span>${escapeHTML(event.title ?? event.code)}</span>
      <time>${clock(event.elapsed_s ?? 0)}</time></li>`).join('');
  }

  /** 계측 데이터가 늘었을 때만, 프레임당 최대 한 번 차트를 다시 그립니다. */
  function scheduleCharts(view, force = false) {
    state.pendingView = view;
    const signature = view
      ? `${view.samples?.length ?? 0}:${view.samples?.at(-1)?.elapsed_s ?? 0}:${view.history?.length ?? 0}:${view.history?.at(-1)?.elapsed_s ?? 0}`
      : 'empty';
    if (!force && signature === chartSignature && !chartFrame) return;
    chartSignature = signature;
    if (chartFrame) return;
    chartFrame = requestAnimationFrame(() => {
      chartFrame = 0;
      const target = state.pendingView;
      if (!target) return;
      drawSensorChart(target);
      drawPowerChart(target);
    });
  }

  function drawSensorChart(view) {
    const samples = (view.samples ?? []).filter((sample) => Number.isFinite(sample.elapsed_s));
    const end = Math.max(10, samples.at(-1)?.elapsed_s ?? 0);
    const series = [];
    if (view.material === 'saltwater') {
      series.push({ color: '#b84b23', samples: samples.map((s) => ({ elapsed_s: s.elapsed_s, v: s.conductivity })) });
    } else {
      series.push({ color: '#36745e', samples: samples.map((s) => ({ elapsed_s: s.elapsed_s, v: s.turbidity_top })) });
      series.push({ color: '#b84b23', samples: samples.map((s) => ({ elapsed_s: s.elapsed_s, v: s.turbidity_bottom })) });
    }
    const unit = view.material === 'saltwater' ? 'mS/cm' : 'NTU';
    el.chartSensor.setAttribute('aria-label', `판정 변수 ${unit} 경과시간 그래프`);
    drawChart(el.chartSensor, series.map((item) => ({ ...item, samples: item.samples.filter((s) => Number.isFinite(s.v)) })),
      { key: 'v', unit, min: 0, max: end, formatTime: clock,
        emptyText: view.finished ? '유효한 측정값 없음' : '실험을 시작하면 그래프가 나타납니다' });
  }

  function drawPowerChart(view) {
    const history = (view.history ?? []).filter((point) => Number.isFinite(point.elapsed_s));
    const end = Math.max(10, history.at(-1)?.elapsed_s ?? 0);
    el.chartPower.setAttribute('aria-label', '누적 에너지 Wh 경과시간 그래프');
    drawChart(el.chartPower, [{
      color: '#2c5544',
      samples: history.map((point) => ({ elapsed_s: point.elapsed_s, v: point.energy_wh })),
    }], { key: 'v', unit: 'Wh', min: 0, max: end, formatTime: clock,
      emptyText: view.finished ? '유효한 측정값 없음' : '실험을 시작하면 그래프가 나타납니다' });
  }

  function resetPanels() {
    if (!tilesBuilt) buildTiles();
    for (const def of TILE_DEFS) tweenNumber(tileNodes.get(def.key), NaN);
    el.events.innerHTML = '<li class="muted">실험을 시작하면 판정 이벤트가 쌓입니다.</li>';
    eventSignature = '';
    chartSignature = '';
    updateMix({ mix: 0 });
  }

  function resetRun() {
    state.engine = null;
    state.view = null;
    clearTimeout(state.pollTimer);
    if (state.stepTimer) clearInterval(state.stepTimer);
    state.stepTimer = null;
    setLocked(false);
    el.result.hidden = true;
    el.result.innerHTML = '';
    el.start.disabled = false;
    el.stop.disabled = true;
    el.note.textContent = '';
    resetPanels();
    el.chartSensorLabel.textContent = '판정 변수';
    el.chartPowerLabel.textContent = '누적 에너지 · Wh';
    tank.reset();
    renderPlaceholder();
  }

  function renderPlaceholder() {
    const options = { key: 'v', min: 0, max: 10, formatTime: clock, emptyText: '실험을 시작하면 그래프가 나타납니다' };
    drawChart(el.chartSensor, [], { ...options, unit: 'mS/cm' });
    drawChart(el.chartPower, [], { ...options, unit: 'Wh' });
  }

  function finishRun(manual = false) {
    const view = state.view;
    el.start.disabled = false;
    el.stop.disabled = true;
    setLocked(false);
    clearTimeout(state.pollTimer);
    if (!view) return;
    if (!manual) el.note.textContent = '실험이 끝났습니다. 아래 결과 카드에서 절감률과 판정 시점을 확인하세요.';
    renderResult(view, manual);
  }

  function renderResult(view, manual) {
    const fixed = view.control_mode === 'fixed';
    const point = (fixed ? view.total : view.result) ?? view.total ?? {};
    const total = view.total ?? point;
    const baseline = view.baseline;
    const energy = fixed ? (total.energy_wh ?? 0) : (point.energy_wh ?? 0);
    const saved = baseline && baseline > 0 ? Math.max(0, (1 - energy / baseline) * 100) : null;
    const finishedText = {
      AUTO_STOP_CONFIRMED: '정지 확인 — 목표 균일도에 도달해 자동 정지했습니다.',
      CANDIDATE_UNVERIFIED: '완료 후보만 기록하고 정지하지 않았습니다(FIXED 기준 운전).',
      NO_CANDIDATE: '완료 후보를 얻지 못했습니다. 투입량 대비 회전수가 부족한지 확인하세요.',
      INTERRUPTED: '실험을 중단했습니다.',
    }[view.final_status] ?? '실험이 끝났습니다.';
    const rows = [
      ['물 / 용질', `${fmt(view.water_l, 0)} L / ${fmt(view.solute_g, 0)} g (${fmt(view.concentration, 0)} g/L)`],
      ['제어 모드', MODE_LABELS[view.control_mode] ?? view.control_mode ?? '—'],
      [fixed ? '운전 시간 (FIXED)' : '판정 시각', clock(fixed ? (total.elapsed_s ?? 0) : (point.elapsed_s ?? 0))],
      ['총 수집 시간', clock(total.elapsed_s ?? view.elapsed_s ?? 0)],
      [fixed ? '에너지 (누적)' : '에너지 (판정 시점)', `${fmt(energy, 3)} Wh`],
      ['잔여 불균일 (CV)', `${fmt(point.cv_pct, 2)} %`],
    ];
    const ratio = baseline && baseline > 0 ? Math.max(0.02, Math.min(1, energy / baseline)) : 0;
    const bars = baseline && baseline > 0 ? `
      <div class="sim-bars">
        <div class="sim-bar"><span>FIXED 기준</span><span class="sim-bar-track"><i style="--w:1"></i></span><b>${baseline.toFixed(2)} Wh</b></div>
        <div class="sim-bar is-run"><span>이번 실험</span><span class="sim-bar-track"><i style="--w:${ratio.toFixed(4)}"></i></span><b>${energy.toFixed(3)} Wh</b></div>
      </div>` : '';
    el.result.hidden = false;
    el.result.innerHTML = `
      <div class="sim-result-head">
        <div>
          <span class="label">실험 결과 · ${escapeHTML(LABELS[view.material] ?? view.material ?? '')}</span>
          <h3>${escapeHTML(finishedText)}</h3>
        </div>
        ${saved === null ? '' : `<div class="saving"><b>0%</b><span>FIXED 기준 대비<br>에너지 절감</span></div>`}
      </div>
      <dl class="sim-result-grid">${rows.map(([key, value], index) =>
        `<div style="--i:${index}"><dt>${key}</dt><dd>${escapeHTML(value)}</dd></div>`).join('')}</dl>
      ${bars}
      <p class="sim-result-note">${baseline
        ? `같은 투입량의 FIXED 기준 운전은 ${baseline.toFixed(2)} Wh 로 계산됩니다(고정 400 RPM · 120초 · 자동 정지 없음).`
        : 'FIXED 기준값을 계산하지 못했습니다.'}
        ${manual ? '' : ` 실행 경로: ${view.source === 'local' ? '브라우저 엔진' : '서버(Python·SQLite)'}.`}</p>
      <div class="sim-result-actions">
        <button class="control primary" type="button" id="sim-again">다시 실행</button>
        <a class="text-link" href="#compare">결과 비교로 이동 ↗</a>
      </div>`;
    const savingNode = el.result.querySelector('.saving b');
    if (savingNode && saved !== null) tweenNumber(savingNode, saved, { digits: 0, suffix: '%', duration: 720 });
    el.result.querySelector('#sim-again').addEventListener('click', () => startExperiment());
    if (typeof el.result.scrollIntoView === 'function') {
      el.result.scrollIntoView({ block: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    }
  }

  /* ── 탱크 애니메이션 (60fps) ───────────────────────────────────────── */
  const tank = createTank(el.canvas, () => state.view, () => state.config);
  function paintTankStatic() {
    syncOutputs();
    tank.refresh();
  }

  /* 차트 캔버스 크기가 바뀌면(회전·창 조절) 같은 데이터로 다시 그립니다. */
  if (window.ResizeObserver) {
    try {
      const chartObserver = new ResizeObserver(() => {
        if (state.view) scheduleCharts(state.view, true);
        else renderPlaceholder();
      });
      chartObserver.observe(el.chartSensor);
      chartObserver.observe(el.chartPower);
    } catch {
      // 관측자를 만들 수 없는 환경은 다음 계측 갱신 때 다시 그립니다.
    }
  }

  /* 앱이 해시로 페이지를 바꾸면 비교/재생 화면에서는 시뮬레이터를 숨깁니다. */
  const syncVisibility = () => {
    const page = location.hash.slice(1);
    const hide = page === 'compare' || page === 'replay';
    if (root.hidden !== hide) root.hidden = hide;
    // display:none → block 으로 돌아오면 캔버스 크기를 다시 읽고 프레임을 재개합니다.
    requestAnimationFrame(() => tank.refresh());
  };
  window.addEventListener('hashchange', syncVisibility);
  syncVisibility();
  syncOutputs();
  resetPanels();
  paintTankStatic();
  mountSegments(root);
  paintSegments();
}

/* ── 공용 그리기 도구 ─────────────────────────────────────────────────── */
function fmt(value, digits = 0) {
  return Number.isFinite(value) ? value.toFixed(digits) : '—';
}

function clock(seconds) {
  const value = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(value / 60);
  return `${String(minutes).padStart(2, '0')}:${String(Math.floor(value % 60)).padStart(2, '0')}`;
}

function escapeHTML(value) {
  const chars = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(value).replace(/[&<>"']/g, (char) => chars[char]);
}

/**
 * 교반조 캔버스. 데이터의 mix(0~1)와 rpm 으로 물 높이·입자 분산·임펠러 회전을 그립니다.
 */
function createTank(canvas, getView, getConfig) {
  const ctx = canvas.getContext('2d');
  const state = {
    level: 0,
    targetLevel: 0,
    rpm: 0,
    mix: 0,
    angle: 0,
    particles: [],
    lastFrame: 0,
    lastDraw: 0,
    size: { width: 0, height: 0 },
    scale: 0,
    visible: false,
    fillFrom: 0,
    starting: false,
  };
  const PARTICLES = 120;
  let rafId = 0;

  const reduced = () => prefersReducedMotion();
  const active = () => state.visible && !document.hidden;

  function schedule() {
    if (rafId || !active()) return;
    rafId = requestAnimationFrame(frame);
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    if (rect.width < 1 || rect.height < 1) {
      // 화면에서 숨겨진 동안에는 프레임을 멈춥니다.
      state.visible = false;
      return;
    }
    const width = Math.round(rect.width);
    const height = Math.round(rect.height);
    state.visible = true;
    if (state.size.width === width && state.size.height === height && state.scale === dpr) {
      schedule();
      return;
    }
    state.size = { width, height };
    state.scale = dpr;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ensureParticles();
    schedule();
  }

  function ensureParticles() {
    if (state.particles.length === PARTICLES) return;
    const random = mulberry(42);
    state.particles = Array.from({ length: PARTICLES }, () => ({
      x: random(), y: random(), tx: random(), ty: random(), size: 1.6 + random() * 2.2, offset: random() - 0.5,
    }));
  }

  function reset() {
    state.starting = true;
    state.fillFrom = performance.now();
    state.level = 0;
    state.mix = 0;
    state.rpm = 0;
    state.angle = 0;
    for (const particle of state.particles) {
      particle.x = 0.2 + Math.random() * 0.6;
      particle.y = 0.9 + Math.random() * 0.08;
    }
  }

  function refresh() {
    resize();
    schedule();
  }

  function frame(now) {
    rafId = 0;
    if (!active()) return;
    const stillness = reduced();
    // 모션을 줄이도록 설정된 경우에는 10fps 로만 그립니다.
    if (stillness && now - state.lastDraw < 100) {
      schedule();
      return;
    }
    state.lastDraw = now;
    const dt = Math.min(0.05, (now - (state.lastFrame || now)) / 1000);
    state.lastFrame = now;
    const view = getView?.();
    const config = getConfig?.();
    const capacity = 30;
    state.targetLevel = Math.min(1, Math.max(0.12, (view?.water_l ?? config?.water_l ?? 10) / capacity));
    if (state.starting) {
      const progress = Math.min(1, (now - state.fillFrom) / 1100);
      state.level += (state.targetLevel * easeOut(progress) - state.level) * Math.min(1, dt * 6);
      if (progress >= 1) state.starting = false;
    } else {
      state.level += (state.targetLevel - state.level) * Math.min(1, dt * 4);
    }
    const targetRpm = Number.isFinite(view?.rpm) ? view.rpm : 0;
    state.rpm += (targetRpm - state.rpm) * Math.min(1, dt * 5);
    const targetMix = Number.isFinite(view?.mix) ? view.mix : 0;
    state.mix += (targetMix - state.mix) * Math.min(1, dt * 3);
    if (!stillness) state.angle += (state.rpm / 300) * 2.6 * dt;   // 화면용 회전(초당 회전수 아님)

    draw(now, dt, view, config, !stillness);
    schedule();
  }

  function draw(now, dt, view, config, animate) {
    const { width, height } = state.size;
    if (!width || !height) return;
    const pad = 14;
    const tankTop = pad + 16;
    const tankBottom = height - pad - 22;
    const tankLeft = pad + 6;
    const tankRight = width - pad - 6;
    const innerW = tankRight - tankLeft;
    const waterH = (tankBottom - tankTop) * state.level;
    const surfaceY = tankBottom - waterH;
    const cx = (tankLeft + tankRight) / 2;
    const cy = tankBottom - waterH * 0.55;
    const material = view?.material ?? config?.material ?? 'saltwater';

    ctx.clearRect(0, 0, width, height);
    // 유리 용기
    roundRect(ctx, tankLeft, tankTop, innerW, tankBottom - tankTop, 12);
    ctx.fillStyle = '#fbfbf8';
    ctx.fill();
    ctx.strokeStyle = '#dfe3dc';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    if (waterH > 1) {
      // 액체
      const topColor = material === 'caco3' ? '#eef1ec' : '#e6f0ee';
      const bottomColor = material === 'caco3' ? '#dfe6e2' : '#d3e5e2';
      const gradient = ctx.createLinearGradient(0, surfaceY, 0, tankBottom);
      gradient.addColorStop(0, mixHex(topColor, '#cfdcd6', 1 - state.mix, material));
      gradient.addColorStop(1, mixHex(bottomColor, '#b9cdc6', 1 - state.mix, material));
      ctx.save();
      roundRect(ctx, tankLeft, tankTop, innerW, tankBottom - tankTop, 12);
      ctx.clip();
      ctx.fillStyle = gradient;
      ctx.fillRect(tankLeft, surfaceY, innerW, waterH);
      const phase = animate ? now / 620 : 0.9;
      const amplitude = 0.6 + 0.12 * Math.min(3, state.rpm / 120);
      ctx.beginPath();
      ctx.moveTo(tankLeft, surfaceY);
      for (let x = tankLeft; x <= tankRight; x += 6) {
        ctx.lineTo(x, surfaceY + Math.sin((x / 18) + phase) * amplitude);
      }
      ctx.lineTo(tankRight, surfaceY + 12);
      ctx.lineTo(tankLeft, surfaceY + 12);
      ctx.closePath();
      const sheen = ctx.createLinearGradient(0, surfaceY, 0, surfaceY + 12);
      sheen.addColorStop(0, 'rgba(255,255,255,.62)');
      sheen.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = sheen;
      ctx.fill();
      drawParticles(ctx, dt, tankLeft, tankRight, surfaceY, tankBottom, animate);
      drawImpeller(ctx, cx, tankBottom - 8, tankRight - tankLeft, waterH);
      ctx.restore();
    } else {
      drawImpeller(ctx, cx, tankBottom - 8, tankRight - tankLeft, 0);
    }

    // 용기 벽 눈금 (10 L · 20 L) — 수면 위에서도 보이도록 액체 위에 그립니다.
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = 'rgba(108, 118, 110, .48)';
    ctx.strokeStyle = 'rgba(120, 132, 122, .28)';
    ctx.lineWidth = 1;
    ctx.textAlign = 'right';
    for (const litres of [10, 20]) {
      const tickY = tankBottom - (tankBottom - tankTop) * (litres / 30);
      ctx.beginPath();
      ctx.moveTo(tankRight - 22, tickY);
      ctx.lineTo(tankRight - 6, tickY);
      ctx.stroke();
      ctx.fillText(String(litres), tankRight - 26, tickY + 3);
    }

    // 눈금·라벨
    ctx.font = '11px ui-monospace, monospace';
    ctx.fillStyle = '#6c766e';
    ctx.textAlign = 'right';
    ctx.fillText(`${fmt(view?.water_l ?? config?.water_l ?? 10, 0)} L`, tankRight - 4, tankTop + 14);
    ctx.textAlign = 'left';
    ctx.fillText(`${fmt(view?.rpm ?? 0, 0)} RPM`, tankLeft + 4, tankBottom + 16);
    ctx.textAlign = 'right';
    ctx.fillText(material === 'caco3' ? 'CaCO₃' : 'NaCl', tankRight - 4, tankBottom + 16);
  }

  function drawParticles(context, dt, left, right, surfaceY, bottom, animate) {
    const disperse = state.mix * (0.35 + 0.65 * Math.min(1, state.rpm / 260));
    for (const particle of state.particles) {
      if (animate && Math.random() < 0.008) {
        particle.tx = 0.12 + Math.random() * 0.76;
        particle.ty = 0.08 + Math.random() * 0.84;
      }
      if (disperse > 0.03) {
        particle.x += (particle.tx - particle.x) * disperse * dt * 1.7;
        particle.y += (particle.ty - particle.y) * disperse * dt * 1.7;
      } else {
        particle.y += (0.95 - particle.y) * dt * 1.4;
        particle.x += (0.5 + particle.offset * 0.3 - particle.x) * dt * 0.9;
      }
      // 소용돌이
      const spin = (state.rpm / 300) * 2.2 * dt;
      if (spin > 0) {
        const px = particle.x - 0.5;
        const py = particle.y - 0.5;
        particle.x = 0.5 + px * Math.cos(spin) - py * Math.sin(spin) * 0.55;
        particle.y = 0.5 + px * Math.sin(spin) * 0.55 + py * Math.cos(spin);
      }
      particle.x = Math.min(0.97, Math.max(0.03, particle.x));
      particle.y = Math.min(0.97, Math.max(0.02, particle.y));
      const x = left + particle.x * (right - left);
      const y = surfaceY + particle.y * (bottom - surfaceY);
      const alpha = 0.35 + 0.45 * (1 - particle.y) * state.mix;
      context.beginPath();
      context.arc(x, y, particle.size, 0, Math.PI * 2);
      context.fillStyle = `rgba(176,132,96,${Math.min(0.85, alpha + 0.25)})`;
      context.fill();
    }
  }

  function drawImpeller(context, cx, baseY, width, waterH) {
    const shaft = Math.min(46, waterH * 0.6);
    context.strokeStyle = '#8e9a92';
    context.lineWidth = 3;
    context.beginPath();
    context.moveTo(cx, baseY);
    context.lineTo(cx, baseY - shaft);
    context.stroke();
    const blades = 3;
    const radius = Math.min(34, width * 0.16);
    context.save();
    context.translate(cx, baseY - shaft);
    context.rotate(state.angle * 3);   // 회전수에 따른 회전 — 정지하면 멈춥니다.
    for (let index = 0; index < blades; index += 1) {
      context.rotate((Math.PI * 2) / blades);
      context.beginPath();
      context.moveTo(0, 0);
      context.lineTo(radius, -4);
      context.lineTo(radius, 4);
      context.closePath();
      context.fillStyle = 'rgba(74,105,88,0.85)';
      context.fill();
    }
    context.beginPath();
    context.arc(0, 0, 4, 0, Math.PI * 2);
    context.fillStyle = '#3d5c4b';
    context.fill();
    context.restore();
  }

  if (window.ResizeObserver) {
    try {
      new ResizeObserver(resize).observe(canvas);
    } catch {
      // 관측자를 만들 수 없는 환경은 resize 이벤트로 대신합니다.
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      state.lastFrame = 0;
      schedule();
    }
  });
  window.addEventListener('resize', resize, { passive: true });
  resize();
  ensureParticles();
  reset();
  schedule();
  return { reset, refresh };
}

function easeOut(value) {
  return 1 - (1 - value) ** 3;
}

function mulberry(seed) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function roundRect(context, x, y, width, height, radius) {
  context.beginPath();
  context.moveTo(x + radius, y);
  context.arcTo(x + width, y, x + width, y + height, radius);
  context.arcTo(x + width, y + height, x, y + height, radius);
  context.arcTo(x, y + height, x, y, radius);
  context.arcTo(x, y, x + width, y, radius);
  context.closePath();
}

function mixHex(a, b, ratio, material) {
  const parse = (hex) => [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
  const [r1, g1, b1] = parse(a);
  const [r2, g2, b2] = parse(b);
  const blend = Math.min(1, Math.max(0, ratio * (material === 'caco3' ? 0.85 : 0.6)));
  const channel = (x, y) => Math.round(x + (y - x) * blend);
  return `rgb(${channel(r1, r2)}, ${channel(g1, g2)}, ${channel(b1, b2)})`;
}

// 모듈의 모든 상수·함수가 준비된 뒤에 화면을 시작합니다.
if (root) init();
