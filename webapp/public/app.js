import { drawChart } from './chart.js';

const main = document.querySelector('#main');
const tabs = [...document.querySelectorAll('[data-page]')];
const metrics = {
  rpm: { label: '회전수', unit: 'RPM', precision: 0, mark: '↻', detail: '모의 회전 센서' },
  conductivity: { label: '전도도', unit: 'mS/cm', precision: 2, mark: '∿', detail: '모의 전도도 센서' },
  turbidity_top: { label: '상부 탁도', unit: 'NTU', precision: 1, mark: '↑', detail: '모의 탁도 센서' },
  turbidity_bottom: { label: '하부 탁도', unit: 'NTU', precision: 1, mark: '↓', detail: '모의 탁도 센서' },
};
const micMetrics = ['rpm', 'conductivity', 'turbidity_top', 'turbidity_bottom'];
const statuses = {
  RUNNING: '수집 중',
  AUTO_STOP_CONFIRMED: '정지 확인',
  STOP_UNCONFIRMED: '정지 미확인',
  CANDIDATE_RECORDED: '완료 후보 기록',
  CANDIDATE_UNVERIFIED: '미검증 완료 후보',
  NO_CANDIDATE: '완료 후보 없음',
  INTERRUPTED: '사용자 중단',
  ABORTED_ERROR: '오류로 중단',
};
const decisions = {
  WAITING_START: '교반 시작 대기',
  BLOCKED: '판정 차단',
  MIN_STIR: '최소 교반 시간 대기',
  NOT_MET: '판정 조건 미충족',
  HOLDING: '판정 조건 유지 중',
  CANDIDATE: '완료 후보',
  MET_AFTER_CANDIDATE: '완료 후보 이후 조건 충족',
};
const palette = ['#b84b23', '#36745e', '#6658a1', '#536d8c', '#88465f', '#745018'];
const colorFor = (index) => palette[index % palette.length];
const archives = new Map();
const pending = new Set();
const unavailable = new Set();
let snapshot;
let offline = false;
let page;
let metricKey = 'conductivity';
let period = 'all';
let recordingIndex = 0;
let replayTime = 0;
let replayTimer;
let selectedRuns = new Set([0, 1]);
let manualId;
let failures = 0;

const isMock = () => snapshot?.mode === 'mock';
const displayMetric = (key, run) => ({
  ...metrics[key],
  unit: isMock() ? metrics[key].unit : run?.units?.[key] ?? '',
});

function archive(index) {
  const record = snapshot.recordings[index];
  if (!record) return null;
  return archives.get(record.id) ?? (record.id === snapshot.live?.id ? snapshot.live : record);
}

async function loadArchive(index) {
  const run = archive(index);
  if (!run || run.samples || pending.has(run.id)) return;
  pending.add(run.id);
  unavailable.delete(run.id);
  try {
    const response = await fetch(`/api/batch?id=${encodeURIComponent(run.id)}`, { signal: AbortSignal.timeout(7000) });
    if (!response.ok) throw new Error();
    archives.set(run.id, await response.json());
  } catch {
    unavailable.add(run.id);
  } finally {
    pending.delete(run.id);
    if (page === 'compare') {
      compareRows();
      refreshChart();
    } else if (page === 'replay' && recordingIndex === index) {
      updateReplay();
    }
  }
}

function escapeHTML(value) {
  const chars = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(value).replace(/[&<>"']/g, (char) => chars[char]);
}

function formatTime(seconds) {
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, '0')}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

function valueText(value, metric) {
  return Number.isFinite(value) ? value.toFixed(metric.precision) : '—';
}

function header(title, subtitle, live = false) {
  return `<section class="hero">
    <div>
      <div class="device">MIC / 교반 모니터</div>
      <h1>${title}</h1><p class="subtitle">${subtitle}</p>
    </div>
    ${live ? '<div id="freshness" class="freshness" role="status"></div>' : ''}
  </section>`;
}

function metricSelect() {
  return `<select id="metric" class="control" aria-label="센서 항목">
    ${micMetrics.map((key) =>
      `<option value="${key}" ${key === metricKey ? 'selected' : ''}>${metrics[key].label}</option>`).join('')}
  </select>`;
}

function eventList(events) {
  if (!events.length) return '<p class="subtitle">표시할 이벤트가 없습니다.</p>';
  return `<ol class="events">${[...events].reverse().map(({ elapsed_s, title, detail }) =>
    `<li><span class="dot" aria-hidden="true"></span>
      <div><span class="event-name">${escapeHTML(title)}</span><small class="event-detail">${escapeHTML(detail)}</small></div>
      <time class="event-time">${Number.isFinite(elapsed_s) ? formatTime(elapsed_s) : '—'}</time></li>`).join('')}</ol>`;
}

function card(label, mark, value, unit, details) {
  return `<article class="card"><span class="label"><span class="metric-mark" aria-hidden="true">${mark}</span>${label}</span>
    <b>${value}</b>${unit ? `<span class="unit">${escapeHTML(unit)}</span>` : ''}<small>${escapeHTML(details)}</small></article>`;
}

function entriesPanel(id) {
  return `<section class="panel records"><h2>수동 기록</h2><div id="${id}"></div></section>`;
}

function entriesList(run) {
  const rows = (run?.entries ?? []).map((entry) => {
    const summary = entry.kind === 'sample'
      ? [entry.position, entry.metric, entry.value, entry.unit].filter((value) => value !== null).join(' · ')
      : entry.kind === 'quality' ? `품질 ${entry.passed === 1 ? '합격' : '불합격'}` : '수동 정지 시각 기록';
    const time = entry.measured_at_utc ? new Date(entry.measured_at_utc).toLocaleString('ko-KR') : '—';
    return `<li><span class="source-badge">${escapeHTML(entry.source)}</span><div>
      <strong>${escapeHTML(summary)}</strong>
      ${entry.note ? `<small>${escapeHTML(entry.note)}</small>` : ''}
      </div><time>${escapeHTML(time)}</time></li>`;
  }).join('');
  return rows ? `<ul class="entry-list">${rows}</ul>` : '<p class="subtitle">수동 기록이 없습니다.</p>';
}

function updateEntries(id, run) {
  const list = document.getElementById(id);
  if (list) list.innerHTML = entriesList(run);
}

function manualForm() {
  return `<section class="panel records">
    <h2>현장 수동 입력</h2>
    <form id="manual-form" class="manual-form">
      <label>기록 종류<select class="control" name="kind" id="entry-kind">
        <option value="sample">시료 측정</option><option value="quality">품질 결과</option>
        <option value="manual_stop">수동 정지 시각 기록</option>
      </select></label>
      <fieldset id="sample-fields">
        <label>측정 항목<input class="control" name="metric" required maxlength="60" placeholder="예: temperature"></label>
        <label>위치<select class="control" name="position"><option value="">선택</option>
          <option value="top">상부</option><option value="mid">중간</option><option value="bottom">하부</option>
        </select></label>
        <label>측정값<input class="control" name="value" type="number" step="any" required></label>
        <label>단위<input class="control" name="unit" maxlength="30"></label>
      </fieldset>
      <fieldset id="quality-fields" disabled hidden>
        <label>품질 결과<select class="control" name="passed"><option value="yes">합격</option><option value="no">불합격</option></select></label>
      </fieldset>
      <label>측정·정지 시각<input class="control" name="measured_at" type="datetime-local"></label>
      <label>메모<input class="control" name="note" maxlength="500"></label>
      <label>입력 PIN<input class="control" name="pin" type="password" inputmode="numeric" pattern="[0-9]{6,12}" required autocomplete="off"></label>
      <button class="control primary" type="submit">기록 저장</button>
      <p id="manual-status" class="subtitle" role="status"></p>
    </form>
  </section>`;
}

function updateEntryForm() {
  const kind = document.querySelector('#entry-kind').value;
  for (const [field, activeKind] of [['sample-fields', 'sample'], ['quality-fields', 'quality']]) {
    const group = document.getElementById(field);
    group.hidden = group.disabled = kind !== activeKind;
  }
}

main.addEventListener('submit', async (event) => {
  const form = event.target;
  if (form.id !== 'manual-form') return;
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  const status = form.querySelector('#manual-status');
  if (!globalThis.crypto?.randomUUID) {
    status.textContent = '수동 기록을 입력하려면 HTTPS로 열어 주세요.';
    return;
  }
  const inputs = Object.fromEntries(new FormData(form));
  const record = {
    batchId: snapshot.live.id, id: manualId ??= crypto.randomUUID(),
    kind: inputs.kind, pin: inputs.pin, note: inputs.note,
    position: inputs.position, metric: inputs.metric, unit: inputs.unit,
    value: inputs.kind === 'sample' ? Number(inputs.value) : null,
    passed: inputs.passed === 'yes',
    measuredAt: inputs.measured_at ? new Date(inputs.measured_at).toISOString() : null,
  };
  const { id } = record;
  status.textContent = '저장 중입니다.';
  button.disabled = true;
  try {
    const response = await fetch('/api/manual', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(record), signal: AbortSignal.timeout(7000),
    });
    const errors = {
      400: '기록 값을 확인해 주세요.',
      403: 'PIN 또는 페이지 주소를 확인해 주세요.',
      404: '해당 배치가 없습니다.',
      429: 'PIN 확인을 여러 번 실패했습니다. 잠시 후 다시 시도해 주세요.',
    };
    if (!response.ok) {
      status.textContent = errors[response.status] ?? '저장 결과를 확인하지 못했습니다. 다시 시도해 주세요.';
      return;
    }
    if (manualId === id) manualId = undefined;
    form.reset();
    if (form.isConnected) updateEntryForm();
    archives.delete(record.batchId);
    status.textContent = '웹 기록을 저장했습니다.';
  } catch {
    status.textContent = '저장 결과를 확인하지 못했습니다. 다시 시도해 주세요.';
  } finally {
    button.disabled = false;
  }
});

function updateLive() {
  const { live, received_at } = snapshot;
  if (!live) return;
  const latest = live.samples.at(-1);
  const codes = new Set(live.events.map((event) => event.code));
  const complete = isMock() ? live.state === 'complete' : live.final_status === 'AUTO_STOP_CONFIRMED' || codes.has('stop_confirmed');
  const stale = !isMock() && live.final_status === 'RUNNING' &&
    Date.now() - Date.parse(live.last_reading_at ?? live.created_at) > 8000;
  const freshness = document.querySelector('#freshness');
  const timestamp = received_at ? new Date(received_at).toLocaleTimeString('ko-KR', { hour12: false }) : '—';
  const freshnessLabel = snapshot.mode === 'neon' ? '마지막 업로드' : '마지막 수신';
  const liveCondition = offline ? '조회 연결 끊김 · ' : stale ? '센서 갱신 지연 · ' : '';
  freshness.innerHTML = `<span class="dot ${offline || stale ? 'offline' : ''}" aria-hidden="true"></span>
    ${liveCondition}${freshnessLabel} <time>${timestamp}</time>`;
  const status = document.querySelector('#batch-status');
  let statusText = complete ? (isMock() ? '교반 완료' : '정지 확인') : '교반 중';
  if (!isMock() && !complete) {
    statusText = statuses[live.final_status] ?? live.final_status;
    if (live.final_status === 'RUNNING') {
      if (codes.has('stop_unconfirmed')) statusText = '정지 미확인';
      else if (codes.has('candidate')) statusText = '완료 후보 기록';
      else if (!codes.has('batch_started')) statusText = '교반 시작 대기';
    }
  }
  const statusMessage = offline || stale ? `마지막 상태 · ${statusText}` : statusText;
  if (status.textContent.trim() !== statusMessage) {
    status.classList.toggle('complete', complete && !offline && !stale);
    status.innerHTML = `<span class="dot" aria-hidden="true"></span>${escapeHTML(statusMessage)}`;
  }
  if (isMock()) {
    document.querySelector('#run-time').textContent = `${formatTime(live.elapsed_s)} / ${formatTime(live.duration_s)}`;
    const progress = document.querySelector('#run-progress');
    progress.max = live.duration_s;
    progress.value = live.elapsed_s;
  } else {
    const decision = live.current_decision;
    const material = { saltwater: '소금물', caco3: '탄산칼슘' }[live.material] ?? live.material;
    document.querySelector('#run-time').textContent = [material, live.mode].join(' · ');
    const candidate = live.events.findLast((event) => event.code === 'candidate');
    document.querySelector('#run-decision').textContent = candidate ? `완료 후보 · ${candidate.detail}`
      : decision ? `${decisions[decision.state] ?? decision.state} · ${decision.verified === 1 ? '검증됨' : '미검증'}`
      : '판정 기록 대기';
  }
  const keys = isMock() ? micMetrics : live.metrics;
  document.querySelector('#stats').innerHTML = keys.map((key) => {
    const metric = displayMetric(key, live);
    const value = isMock() ? latest?.[key] : live.latest[key]?.value;
    const status = live.latest?.[key]?.status;
    const details = Number.isFinite(value) ? (isMock() ? metric.detail : 'MIC 센서 기록')
      : status ? `유효한 측정값 없음 · ${status}` : '유효한 측정값 없음';
    return card(metric.label, metric.mark, valueText(value, metric), metric.unit, details);
  }).join('') + card('경과시간', '◷', formatTime(live.elapsed_s), '',
    isMock() ? `목표 ${formatTime(live.duration_s)}` : '수집 시작 기준');
  document.querySelector('#live-events').innerHTML = eventList(live.events);
  updateEntries('live-entries', live);
  refreshChart();
}

function renderLive() {
  manualId = undefined;
  const title = page === 'live' ? '실시간 교반 상태' : '교반 대시보드';
  if (!snapshot.live) {
    main.innerHTML = `${header(title, 'MIC 배치 기록을 기다리는 중입니다.')}
      <p class="panel placeholder">수신한 배치가 없습니다.</p>`;
    return;
  }
  main.innerHTML = `${header(title, '현재 배치의 센서 값과 교반 완료 여부를 확인하세요.', true)}
    <section class="run-bar" aria-label="현재 배치">
      <div class="batch-info"><span class="label">현재 배치</span><strong class="batch-code">${escapeHTML(snapshot.live.id)}</strong></div>
      <div class="progress-area">
        <div class="progress-caption"><span class="label">${isMock() ? '교반 진행률' : '배치 정보'}</span><strong id="run-time"></strong></div>
        ${isMock() ? '<progress id="run-progress" aria-label="교반 진행률"></progress>' : '<span class="label" id="run-decision"></span>'}
      </div>
      <div id="batch-status" class="status" role="status"></div>
    </section>
    <section id="stats" class="stats" aria-label="최신 센서 값"></section>
    <div class="dashboard">
      <section class="panel">
        <div class="panel-head"><div><h2>센서 추이</h2><p class="subtitle">수집 시작 기준 경과시간 (분:초)</p></div>${metricSelect()}</div>
        <div class="chart-top">
          <span class="label"><span class="legend-line"></span><span id="metric-legend"></span></span>
          <div class="segment" aria-label="표시 구간">
            <button type="button" data-period="all" aria-pressed="${period === 'all'}">전체</button>
            <button type="button" data-period="recent" aria-pressed="${period === 'recent'}">최근 2분</button>
          </div>
        </div>
        <canvas id="chart" class="chart ${page === 'live' ? 'wide-chart' : ''}" role="img"></canvas>
      </section>
      <section class="panel">
        <div class="panel-head"><div><h2>배치 타임라인</h2><p class="subtitle">교반 중 기록된 주요 이벤트</p></div></div>
        <div id="live-events"></div>
        ${page === 'home' ? '<div class="bottom-link"><a class="text-link" href="#live">실시간 상세 <span aria-hidden="true">&nbsp;↗</span></a></div>' : ''}
      </section>
    </div>
    ${isMock() ? '' : entriesPanel('live-entries')}
    ${snapshot.manual_enabled ? manualForm() : ''}`;
  updateLive();
}

function compareRows() {
  const runs = [...selectedRuns].map(archive).filter(Boolean);
  const rows = runs.map((run) => {
    const metric = displayMetric(metricKey, run);
    const values = (run.samples ?? []).map((sample) => sample[metricKey]).filter(Number.isFinite);
    const mean = values.length ? values.reduce((total, value) => total + value, 0) / values.length : NaN;
    const max = values.reduce((highest, value) => Math.max(highest, value), -Infinity);
    const loading = unavailable.has(run.id) ? '기록 로드 실패' : '불러오는 중…';
    if (!run.samples) return `<tr><td>${escapeHTML(run.id)}</td><td colspan="4">${loading}</td></tr>`;
    const mode = run.mode ? ` <span class="source-badge">${escapeHTML(run.mode)}</span>` : '';
    return `<tr><td>${escapeHTML(run.id)}${mode}</td><td>${formatTime(run.duration_s)}</td>
      <td>${valueText(mean, metric)}</td><td>${valueText(max, metric)}</td><td>${escapeHTML(metric.unit || '—')}</td></tr>`;
  }).join('');
  document.querySelector('#compare-table').innerHTML = rows ? `<table>
    <thead><tr><th scope="col">배치</th><th scope="col">기록 구간</th><th scope="col">평균</th><th scope="col">최대</th><th scope="col">단위</th></tr></thead>
    <tbody>${rows}</tbody></table>` : '<p class="subtitle">비교할 배치를 선택하세요.</p>';
  const manual = document.querySelector('#compare-entries');
  if (manual) {
    manual.innerHTML = runs.filter((run) => run.entries?.length).map((run) =>
      `<section class="record-group"><h3>${escapeHTML(run.id)}</h3>${entriesList(run)}</section>`).join('')
      || entriesList(null);
  }
}

function renderCompare() {
  main.innerHTML = `${header('결과 비교', '기록을 마친 배치를 같은 경과시간 기준으로 비교하세요.')}
    <section class="panel">
      <div class="panel-head"><h2>배치별 센서 추이</h2>${metricSelect()}</div>
      <div id="compare-picks" class="picks"></div>
      <p class="subtitle" id="chart-note" hidden></p>
      <canvas id="chart" class="chart wide-chart" role="img"></canvas>
      <div id="compare-table" class="table-scroll"></div>
    </section>
    ${isMock() ? '' : entriesPanel('compare-entries')}`;
  updateRecordings();
  refreshChart();
}

function renderReplay() {
  main.innerHTML = `${header('녹화 재생', '시간 막대를 움직여 교반 과정과 이벤트를 다시 확인하세요.')}
    <section class="panel">
      <div class="panel-head"><h2>배치 재생</h2>
        <select id="recording" class="control" aria-label="재생할 배치"></select>
        ${metricSelect()}
      </div>
      <canvas id="chart" class="chart wide-chart" role="img"></canvas>
      <div class="replay-controls">
        <button id="play" class="control primary" type="button">재생</button>
        <input id="scrub" type="range" min="0" step="1" value="0" aria-label="재생 경과시간">
        <span id="replay-time" class="replay-time"></span>
      </div>
      <p id="replay-value" class="subtitle"></p>
    </section>
    <section class="panel replay-events"><h2>재생 구간 이벤트</h2><div id="replay-events"></div></section>
    ${isMock() ? '' : entriesPanel('replay-entries')}`;
  updateRecordings();
}

function updateRecordings() {
  if (page === 'compare') {
    const picks = document.querySelector('#compare-picks');
    for (const [index, run] of snapshot.recordings.entries()) {
      if (picks.querySelector(`[data-run="${index}"]`)) continue;
      picks.insertAdjacentHTML('beforeend', `<label class="pick">
        <input type="checkbox" data-run="${index}" ${selectedRuns.has(index) ? 'checked' : ''}>
        <span class="legend-line" style="--legend:${colorFor(index)}"></span>${escapeHTML(run.id)}
        </label>`);
    }
    compareRows();
    refreshChart();
    for (const index of selectedRuns) loadArchive(index);
  } else if (page === 'replay') {
    const select = document.querySelector('#recording');
    for (const [index, run] of snapshot.recordings.entries()) {
      if (!select.options[index]) select.add(new Option(run.mode ? `${run.id} · ${run.mode}` : run.id, String(index)));
    }
    select.value = String(recordingIndex);
    updateReplay();
    loadArchive(recordingIndex);
  }
}

function updateReplay() {
  const run = archive(recordingIndex);
  const slider = document.querySelector('#scrub');
  const play = document.querySelector('#play');
  const description = document.querySelector('#replay-value');
  slider.disabled = play.disabled = !run?.samples?.length;
  if (!run?.samples) {
    document.querySelector('#replay-time').textContent = '—';
    description.textContent = !run ? '재생할 배치가 없습니다.'
      : unavailable.has(run.id) ? '기록을 불러오지 못했습니다. 배치를 다시 선택하세요.' : '기록을 불러오는 중입니다.';
    document.querySelector('#replay-events').innerHTML = '';
    updateEntries('replay-entries', null);
    refreshChart();
    return;
  }
  slider.max = run.duration_s;
  slider.value = replayTime;
  slider.setAttribute('aria-valuetext', formatTime(replayTime));
  document.querySelector('#replay-time').textContent = `${formatTime(replayTime)} / ${formatTime(run.duration_s)}`;
  play.textContent = replayTimer ? '일시정지' : '재생';
  const sample = run.samples.findLast((point) => point.elapsed_s <= replayTime && Object.hasOwn(point, metricKey));
  const metric = displayMetric(metricKey, run);
  const value = sample?.[metricKey];
  description.textContent =
    `${metric.label} · ${Number.isFinite(value) ? `${valueText(value, metric)} ${metric.unit}` : '유효한 측정값 없음'}`;
  document.querySelector('#replay-events').innerHTML = eventList(run.events.filter(({ elapsed_s }) =>
    Number.isFinite(elapsed_s) && elapsed_s <= replayTime));
  updateEntries('replay-entries', run);
  refreshChart();
}

function stopReplay() {
  clearInterval(replayTimer);
  replayTimer = undefined;
}

function refreshChart() {
  const canvas = document.querySelector('#chart');
  if (!canvas || !snapshot) return;
  let metric = displayMetric(metricKey, snapshot.live);
  const legend = document.querySelector('#metric-legend');
  if (legend) legend.textContent = [metric.label, metric.unit].filter(Boolean).join(' · ');
  let runs;
  let min = 0;
  let max;
  let cursor;
  if (page === 'compare') {
    runs = [...selectedRuns].flatMap((index) => {
      const run = archive(index);
      return run?.samples?.some((sample) => Number.isFinite(sample[metricKey]))
        ? [{ ...run, color: colorFor(index) }] : [];
    });
    const units = new Set(runs.map((run) => displayMetric(metricKey, run).unit));
    const note = document.querySelector('#chart-note');
    note.textContent = '선택한 배치의 센서 단위가 달라 같은 축에 표시할 수 없습니다.';
    note.hidden = units.size <= 1;
    if (units.size > 1) runs = [];
    metric = displayMetric(metricKey, runs[0]);
    max = Math.max(2, ...runs.map(({ duration_s }) => duration_s));
  } else if (page === 'replay') {
    const run = archive(recordingIndex);
    metric = displayMetric(metricKey, run);
    max = Math.max(2, run?.duration_s ?? 0);
    runs = run?.samples ? [run] : [];
    cursor = replayTime;
  } else {
    const { live } = snapshot;
    max = Math.max(2, live.samples.at(-1)?.elapsed_s ?? 0);
    if (period === 'recent') min = Math.max(0, max - 120);
    runs = [live];
  }
  canvas.setAttribute('aria-label', `${[metric.label, metric.unit].filter(Boolean).join(' · ')} 경과시간 그래프`);
  drawChart(canvas, runs.map(({ samples, color = palette[0] }) => ({
    samples: samples.filter((point) => Object.hasOwn(point, metricKey)), color,
  })),
    { key: metricKey, unit: metric.unit, min, max, formatTime, cursor });
}

function renderPage() {
  const requested = location.hash.slice(1);
  page = tabs.some(({ dataset }) => dataset.page === requested) ? requested : 'home';
  for (const tab of tabs) {
    if (tab.dataset.page === page) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }
  if (!snapshot) return;
  if (page === 'compare') renderCompare();
  else if (page === 'replay') renderReplay();
  else renderLive();
}

main.addEventListener('change', (event) => {
  const { target } = event;
  if (target.id === 'metric') {
    metricKey = target.value;
    if (page === 'compare') compareRows();
    if (page === 'replay') updateReplay();
    else refreshChart();
  } else if (target.id === 'recording') {
    stopReplay();
    recordingIndex = Number(target.value);
    replayTime = 0;
    updateReplay();
    loadArchive(recordingIndex);
  } else if (target.dataset.run !== undefined) {
    const index = Number(target.dataset.run);
    if (target.checked) selectedRuns.add(index);
    else selectedRuns.delete(index);
    compareRows();
    refreshChart();
    if (target.checked) loadArchive(index);
  } else if (target.id === 'entry-kind') {
    updateEntryForm();
  }
});

main.addEventListener('input', ({ target }) => {
  if (target.form?.id === 'manual-form' && target.name !== 'pin') manualId = undefined;
  if (target.id !== 'scrub') return;
  replayTime = Number(target.value);
  updateReplay();
});

main.addEventListener('click', ({ target }) => {
  if (target.dataset.period) {
    period = target.dataset.period;
    for (const button of main.querySelectorAll('[data-period]')) {
      button.setAttribute('aria-pressed', String(button.dataset.period === period));
    }
    refreshChart();
  } else if (target.id === 'play') {
    const duration = archive(recordingIndex).duration_s;
    if (replayTimer) stopReplay();
    else {
      if (replayTime === duration) replayTime = 0;
      replayTimer = setInterval(() => {
        replayTime = Math.min(duration, replayTime + 4);
        if (replayTime === duration) stopReplay();
        updateReplay();
      }, 1000);
    }
    updateReplay();
  }
});

window.addEventListener('hashchange', () => {
  stopReplay();
  renderPage();
});
new ResizeObserver(refreshChart).observe(main);

async function poll() {
  try {
    const response = await fetch('/api/snapshot', { cache: 'no-store', signal: AbortSignal.timeout(7000) });
    if (!response.ok) throw new Error('Mock API failed');
    const data = await response.json();
    const first = !snapshot;
    const newBatch = snapshot?.live?.id !== data.live?.id;
    if (first && data.mode !== 'mock') metricKey = 'rpm';
    // Retain finished mock recordings on server reconnect until the browser reloads.
    const recordings = new Map(snapshot?.recordings.map((run) => [run.id, run]));
    for (const run of data.recordings) recordings.set(run.id, run);
    const hasNewRecording = recordings.size !== snapshot?.recordings.length;
    snapshot = { ...data, recordings: [...recordings.values()] };
    const mockData = data.mode === 'mock' || data.live?.mode === 'mock';
    const mode = document.querySelector('.mode');
    mode.textContent = mockData ? 'MOCK' : 'MIC';
    mode.hidden = false;
    document.querySelector('#data-mode').textContent = [
      mockData ? '모의 데이터' : 'MIC 데이터',
      data.manual_enabled ? '웹 수동 기록 사용' : '읽기 전용',
    ].join(' · ');
    offline = false;
    failures = 0;
    main.setAttribute('aria-busy', 'false');
    if (first) renderPage();
    else if (page === 'home' || page === 'live') {
      if (newBatch) renderPage();
      else updateLive();
    }
    else if (hasNewRecording) updateRecordings();
  } catch {
    offline = true;
    failures += 1;
    if (!snapshot) {
      main.setAttribute('aria-busy', 'false');
      main.innerHTML = failures > 1
        ? `<p class="placeholder" role="status">이 주소는 조회 API 없이 배포된 화면입니다. 위쪽 <b>실시간 실험</b>에서 실험을 실행하면 이 자리 대신 결과가 쌓입니다.
          MIC 원본 기록까지 보려면 <code>node webapp/server.mjs</code>로 직접 실행하세요.</p>`
        : '<p class="placeholder" role="status">교반 데이터를 불러오는 중입니다.</p>';
    } else if (page === 'home' || page === 'live') updateLive();
  } finally {
    setTimeout(poll, document.hidden ? 15000 : 2000);
  }
}

renderPage();
poll();
