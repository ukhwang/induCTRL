/**
 * 화면 공통 마이크로 인터랙션 도구.
 *
 *  · 세그먼트 컨트롤(FIXED/RULE/AI, 1x/5x/10x, 전체/최근 2분)의 선택 표시가
 *    버튼 사이를 미끄러지도록 `.seg-thumb` 를 붙이고 위치를 맞춥니다.
 *    transform/width 만 움직이므로 레이아웃을 다시 계산하지 않습니다.
 *  · 계측 숫자는 목표값으로 부드럽게 이어지도록(tweenNumber) 한 프레임 루프에서 갱신합니다.
 *  · prefers-reduced-motion 을 읽어 모션을 끌지 판단합니다.
 */

const motionQuery = typeof window.matchMedia === 'function'
  ? window.matchMedia('(prefers-reduced-motion: reduce)')
  : null;

export const prefersReducedMotion = () => Boolean(motionQuery?.matches);

/* ── 숫자 전환 ─────────────────────────────────────────────────────────── */
const tweens = new Map();
let tweenFrame = 0;

function writeNumber(node, value, digits, suffix) {
  node.textContent = `${value.toFixed(digits)}${suffix}`;
}

function tickTweens(now) {
  tweenFrame = 0;
  for (const [node, state] of tweens) {
    // 시작 시각은 첫 프레임의 타임스탬프로 잡습니다. rAF 시계와 performance.now() 가
    // 다른 환경에서도 값이 어긋나지 않습니다.
    if (state.start === null) state.start = Number.isFinite(now) ? now : 0;
    const elapsed = Number.isFinite(now) ? now - state.start : 0;
    const progress = state.duration > 0 ? Math.min(1, Math.max(0, elapsed / state.duration)) : 1;
    const eased = 1 - (1 - progress) ** 3;
    state.value = state.from + (state.to - state.from) * eased;
    writeNumber(node, state.value, state.digits, state.suffix);
    if (progress >= 1) tweens.delete(node);
  }
  if (tweens.size) tweenFrame = requestAnimationFrame(tickTweens);
}

/**
 * 숫자 텍스트를 목표값까지 부드럽게 이동시킵니다.
 * 값이 유효하지 않으면 '—' 로 둡니다.
 */
export function tweenNumber(node, target, { digits = 0, duration = 260, suffix = '' } = {}) {
  if (!node) return;
  if (!Number.isFinite(target)) {
    tweens.delete(node);
    node.removeAttribute('data-value');
    node.textContent = '—';
    return;
  }
  const previous = tweens.get(node);
  const from = previous ? previous.value : Number(node.dataset.value ?? '0');
  const start = Number.isFinite(from) ? from : target;
  if (prefersReducedMotion() || duration <= 0) {
    tweens.delete(node);
    node.dataset.value = String(target);
    writeNumber(node, target, digits, suffix);
    return;
  }
  node.dataset.value = String(target);
  tweens.set(node, { from: start, to: target, start: null, duration, digits, suffix, value: start });
  writeNumber(node, start, digits, suffix);
  if (!tweenFrame) tweenFrame = requestAnimationFrame(tickTweens);
}

/* ── 세그먼트 선택 표시 ────────────────────────────────────────────────── */
const thumbOf = new WeakMap();
let paintQueued = false;

function paintSegment(segment) {
  const thumb = thumbOf.get(segment);
  if (!thumb) return;
  const active = segment.querySelector('button[aria-pressed="true"]');
  if (!active) {
    thumb.style.opacity = '0';
    return;
  }
  // 레이아웃이 아직 없는 환경(jsdom, 숨겨진 화면)에서는 표시하지 않습니다.
  if (!active.offsetWidth || !active.offsetHeight) {
    thumb.style.opacity = '0';
    return;
  }
  thumb.style.opacity = '1';
  thumb.style.width = `${active.offsetWidth}px`;
  thumb.style.height = `${active.offsetHeight}px`;
  thumb.style.transform = `translate3d(${active.offsetLeft}px, ${active.offsetTop}px, 0)`;
}

function paintAll() {
  paintQueued = false;
  for (const segment of document.querySelectorAll('.segment')) paintSegment(segment);
}

/** 다음 프레임에 한 번만 다시 그립니다. */
export function paintSegments() {
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(paintAll);
}

/** 아직 표시가 없는 세그먼트에 `.seg-thumb` 를 붙입니다. */
export function mountSegments(root = document) {
  for (const segment of root.querySelectorAll('.segment')) {
    if (thumbOf.has(segment)) continue;
    const thumb = document.createElement('span');
    thumb.className = 'seg-thumb';
    thumb.setAttribute('aria-hidden', 'true');
    segment.prepend(thumb);
    thumbOf.set(segment, thumb);
    if (typeof ResizeObserver === 'function') {
      try {
        new ResizeObserver(() => paintSegment(segment)).observe(segment);
      } catch {
        // ResizeObserver 가 없는 환경은 아래 MutationObserver·resize 로 충분합니다.
      }
    }
  }
  paintSegments();
}

function watchSegments() {
  if (typeof MutationObserver !== 'function') return;
  const observer = new MutationObserver((records) => {
    let mounted = false;
    for (const record of records) {
      if (record.type === 'attributes') {
        paintSegments();
        return;
      }
      // 계측 숫자처럼 글자만 바뀌는 경우까지 훑지 않도록, 새로 붙은 요소 중
      // 세그먼트를 담은 것만 확인합니다.
      if (mounted) continue;
      for (const node of record.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.matches?.('.segment') || node.querySelector?.('.segment')) {
          mounted = true;
          break;
        }
      }
    }
    if (mounted) mountSegments();
  });
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['aria-pressed'],
  });
}

if (typeof document !== 'undefined') {
  mountSegments();
  watchSegments();
  window.addEventListener('resize', paintSegments, { passive: true });
  window.addEventListener('load', paintSegments, { once: true });
  document.fonts?.ready?.then(paintSegments).catch(() => {});
}
