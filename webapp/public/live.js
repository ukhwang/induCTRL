/**
 * 전시장 실제 교반기 제어 패널.
 *
 *   폰 → POST /api/command → Neon stir_commands → MIC(mic/agent.py) → 실제 모터
 *   폰 ← GET  /api/command/status  (명령 상태 · MIC 온라인 · 최근 명령)
 *
 * DB 가 없는 배포(/api/command 가 없거나 sim_only)에서는 패널을 숨기고
 * 위쪽 시뮬레이터만 그대로 동작합니다. 센서 값은 app.js 의 /api/snapshot 이 담당합니다.
 */
import { mountSegments, paintSegments } from './ui.js';

const panel = document.querySelector('#live-panel');
if (panel) init(panel);

const POLL_ACTIVE_MS = 1500;
const POLL_IDLE_MS = 2500;
const POLL_HIDDEN_MS = 15000;

const STATUS_TEXT = {
  PENDING: 'MIC 로 전달 중',
  RUNNING: '실행 중',
  COMPLETED: '완료',
  FAILED: '실패',
  CANCELED: '취소',
};
const ACTION_TEXT = { START: '시작', STOP: '정지' };

function init(root) {
  const el = {
    device: root.querySelector('#live-device'),
    deviceText: root.querySelector('#live-device-text'),
    form: root.querySelector('#live-form'),
    water: root.querySelector('#live-water'),
    solute: root.querySelector('#live-solute'),
    waterOut: root.querySelector('#live-water-out'),
    soluteOut: root.querySelector('#live-solute-out'),
    rpm: root.querySelector('#live-rpm'),
    rpmOut: root.querySelector('#live-rpm-out'),
    mode: root.querySelector('#live-mode'),
    pinField: root.querySelector('#live-pin-field'),
    pin: root.querySelector('#live-pin'),
    start: root.querySelector('#live-start'),
    stop: root.querySelector('#live-stop'),
    note: root.querySelector('#live-note'),
    state: root.querySelector('#live-state'),
    stateText: root.querySelector('#live-state-text'),
    stateDetail: root.querySelector('#live-state-detail'),
    facts: root.querySelector('#live-facts'),
    history: root.querySelector('#live-history'),
  };

  const state = {
    visible: false,
    mode: null,
    status: null,
    busy: false,
    timer: null,
    pinRequired: false,
    noteKind: '',
    runStartedAt: null,
    detailText: '',
  };
  let elapsedTimer = null;

  const fmtTime = (value) => (value ? new Date(value).toLocaleTimeString('ko-KR', { hour12: false }) : '—');
  const fmtAge = (seconds) => {
    if (!Number.isFinite(seconds)) return '—';
    if (seconds < 1) return '방금';
    if (seconds < 60) return `${Math.round(seconds)}초 전`;
    return `${Math.round(seconds / 60)}분 전`;
  };
  const clock = (seconds) => {
    const value = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
    return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
  };

  /** 운전 중에는 서버를 더 부르지 않고 경과시간만 1초마다 다시 씁니다. */
  function tickElapsed() {
    if (elapsedTimer) return;
    elapsedTimer = setInterval(() => {
      if (!state.runStartedAt) {
        clearInterval(elapsedTimer);
        elapsedTimer = null;
        return;
      }
      if (document.hidden) return;
      const seconds = (Date.now() - state.runStartedAt) / 1000;
      el.stateDetail.textContent = `${state.detailText} · 경과 ${clock(seconds)}`;
    }, 1000);
  }

  const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));

  /* ── 입력 ──────────────────────────────────────────────────────────── */
  const readForm = () => ({
    material: el.form.querySelector('input[name="lmaterial"]:checked')?.value ?? 'saltwater',
    water_l: Number(el.water.value),
    solute_g: Number(el.solute.value),
    control_mode: [...el.mode.querySelectorAll('button')]
      .find((button) => button.getAttribute('aria-pressed') === 'true')?.dataset.mode ?? 'rule',
    target_rpm_pct: Number(el.rpm.value),
  });

  const syncOutputs = () => {
    el.waterOut.textContent = `${el.water.value} L`;
    el.soluteOut.textContent = `${el.solute.value} g`;
    el.rpmOut.textContent = `${el.rpm.value}%`;
  };

  const setNote = (text, kind = '') => {
    if (el.note.textContent !== text) el.note.textContent = text;
    state.noteKind = kind;
    el.note.dataset.kind = kind;
  };

  el.form.addEventListener('input', syncOutputs);
  el.form.addEventListener('change', syncOutputs);
  el.mode.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    for (const other of el.mode.querySelectorAll('button')) other.setAttribute('aria-pressed', 'false');
    button.setAttribute('aria-pressed', 'true');
    paintSegments();
  });
  el.form.addEventListener('submit', (event) => {
    event.preventDefault();
    send('START');
  });
  el.stop.addEventListener('click', () => send('STOP'));

  /* ── 명령 전송 ─────────────────────────────────────────────────────── */
  async function send(action) {
    if (state.busy || state.mode === 'sim_only') return;
    const body = action === 'START' ? { action, ...readForm() } : { action };
    if (state.pinRequired) {
      const pin = el.pin.value.trim();
      if (action === 'START' && !pin) {
        setNote('운영 PIN 을 입력해 주세요.', 'warn');
        el.pin.focus();
        return;
      }
      body.pin = pin;
    }
    state.busy = true;
    paint();
    setNote(action === 'START' ? '시작 신호를 보내는 중입니다…' : '정지 신호를 보내는 중입니다…', 'pending');
    try {
      const response = await fetch('/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(8000),
      });
      const payload = await response.json().catch(() => null);
      if (response.ok && payload?.ok) {
        setNote(payload.message ?? '명령을 보냈습니다.', action === 'STOP' ? 'stop' : 'ok');
      } else {
        setNote(payload?.message ?? '명령을 처리하지 못했습니다.', response.status === 409 ? 'locked' : 'error');
      }
    } catch {
      setNote('명령을 보내지 못했습니다. 네트워크를 확인해 주세요.', 'error');
    } finally {
      state.busy = false;
      await refresh({ immediate: true });
    }
  }

  /* ── 상태 반영 ─────────────────────────────────────────────────────── */
  function paint() {
    const status = state.status;
    const mic = status?.mic ?? null;
    const online = mic?.online === true;
    const active = status?.active ?? null;
    const pending = status?.stop ?? null;
    const running = Boolean(active);

    el.device.dataset.state = !mic ? 'unknown' : online ? 'online' : 'offline';
    el.deviceText.textContent = !mic
      ? 'MIC 연결 확인 중'
      : online
        ? `MIC 온라인 · ${fmtAge(mic.age_s)}`
        : `MIC 오프라인 · ${fmtAge(mic.age_s)}`;

    let stateText = '대기';
    let detail = '교반 시작을 누르면 실제 모터가 돌아갑니다.';
    let kind = 'idle';
    if (!mic) {
      stateText = 'MIC 대기';
      detail = '현장 에이전트(mic/agent.py)가 켜지면 표시됩니다.';
      kind = 'offline';
    } else if (!online) {
      stateText = 'MIC 오프라인';
      detail = '마지막 생존 신호가 오래되었습니다. 시작할 수 없습니다.';
      kind = 'offline';
    } else if (pending) {
      stateText = '정지 처리 중';
      detail = '정지 명령을 MIC 가 받아 처리하고 있습니다.';
      kind = 'stop';
    } else if (active?.status === 'PENDING') {
      stateText = '시작 전달 중';
      detail = 'MIC 가 시작 명령을 받아 모터를 올리는 중입니다.';
      kind = 'pending';
    } else if (active?.status === 'RUNNING') {
      stateText = '현재 운전 중 (잠금)';
      detail = `${active.payload?.material === 'caco3' ? '탄산칼슘' : '소금물'} ${active.payload?.water_l ?? '—'} L · 교반 세기 ${active.payload?.target_rpm_pct ?? '—'}%`;
      kind = 'running';
    } else if (status?.batch?.status === 'RUNNING') {
      stateText = '교반 중';
      detail = `현장 배치 ${status.batch.id} 가 돌고 있습니다.`;
      kind = 'running';
    }
    if (state.stateText !== stateText) el.stateText.textContent = stateText;
    state.stateText = stateText;
    state.detailText = detail;
    state.runStartedAt = kind === 'running' && active?.started_at ? Date.parse(active.started_at) : null;
    const shown = state.runStartedAt
      ? `${detail} · 경과 ${clock((Date.now() - state.runStartedAt) / 1000)}` : detail;
    if (el.stateDetail.textContent !== shown) el.stateDetail.textContent = shown;
    if (state.runStartedAt) tickElapsed();
    el.state.dataset.kind = kind;

    if (el.pinField.hidden === state.pinRequired) el.pinField.hidden = !state.pinRequired;

    el.start.disabled = state.busy || !online || running || !mic;
    el.stop.disabled = state.busy;
    el.form.classList.toggle('is-locked', running);
    el.form.setAttribute('aria-busy', String(state.busy));

    const facts = [];
    if (mic) facts.push(`기기 ${escape(mic.id)}`);
    if (status?.batch?.id) facts.push(`최근 배치 ${escape(status.batch.id)}`);
    if (status?.batch?.created_at) facts.push(`배치 시작 ${fmtTime(status.batch.created_at)}`);
    facts.push(status?.mode === 'local' ? '로컬 시뮬레이터' : 'Neon DB 경유');
    el.facts.innerHTML = facts.map((fact) => `<span>${fact}</span>`).join('');

    const rows = (status?.recent ?? []).map((item) => {
      const label = `${ACTION_TEXT[item.action] ?? item.action} · ${STATUS_TEXT[item.status] ?? item.status}`;
      return `<li data-kind="${escape(item.status)}"><span>${escape(label)}</span>`
        + `<time>${fmtTime(item.created_at)}</time></li>`;
    });
    if (pending) rows.unshift(`<li data-kind="PENDING"><span>정지 · ${STATUS_TEXT[pending.status] ?? pending.status}</span><time>${fmtTime(pending.created_at)}</time></li>`);
    el.history.innerHTML = rows.join('') || '<li data-kind="empty">아직 명령이 없습니다.</li>';
  }

  /* ── 폴링 ──────────────────────────────────────────────────────────── */
  async function refresh({ immediate = false } = {}) {
    clearTimeout(state.timer);
    try {
      const response = await fetch('/api/command/status', { cache: 'no-store', signal: AbortSignal.timeout(6000) });
      if (!response.ok) throw new Error('status');
      const status = await response.json();
      state.status = status;
      state.mode = status.mode ?? 'neon';
      state.pinRequired = status.pin_required === true;
      if (state.mode === 'sim_only') {
        hide();
        return;
      }
      syncVisibility();
      paint();
    } catch {
      // 정적 배포에는 이 API 가 없습니다. 시뮬레이터만 보여 줍니다.
      if (!state.status) {
        state.mode = 'sim_only';
        hide();
      } else {
        el.device.dataset.state = 'offline';
        el.deviceText.textContent = '상태를 읽지 못했습니다';
      }
    } finally {
      const wait = document.hidden ? POLL_HIDDEN_MS
        : state.status?.running || state.status?.stop ? POLL_ACTIVE_MS : POLL_IDLE_MS;
      state.timer = setTimeout(() => refresh(), immediate ? 400 : wait);
    }
  }

  /* ── 화면 표시 ─────────────────────────────────────────────────────── */
  function show() {
    if (!state.visible) {
      state.visible = true;
      root.hidden = false;
      requestAnimationFrame(paintSegments);
    }
  }

  function hide() {
    if (state.visible || root.hidden === false) {
      state.visible = false;
      root.hidden = true;
    }
  }

  const syncVisibility = () => {
    const page = location.hash.slice(1);
    const hiddenPage = page === 'compare' || page === 'replay';
    if (hiddenPage) hide();
    else if (state.mode && state.mode !== 'sim_only') show();
    else if (!state.mode) hide();
  };

  window.addEventListener('hashchange', syncVisibility);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh({ immediate: true });
  });

  mountSegments(root);
  paintSegments();
  syncOutputs();
  syncVisibility();
  refresh();
}
