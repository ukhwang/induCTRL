const tickLabel = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 1 });

/** 캔버스마다 마지막으로 맞춘 픽셀 크기를 기억해, 매 프레임 재할당(전체 지우기·버퍼 생성)을 피합니다. */
const sizes = new WeakMap();

export function drawChart(canvas, series, { key, unit, min, max, formatTime, cursor, emptyText = '유효한 측정값 없음' }) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const scale = window.devicePixelRatio || 1;
  const cached = sizes.get(canvas);
  if (!cached || cached.width !== width || cached.height !== height || cached.scale !== scale) {
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    sizes.set(canvas, { width, height, scale });
  }
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  // transform 으로만 확대하고 지웁니다. canvas.width 재할당과 달리 상태 초기화가 없어
  // 아래에서 쓰는 스타일을 매번 명시합니다.
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const values = series.flatMap(({ samples }) => samples
    .filter((sample) => sample.elapsed_s >= min && sample.elapsed_s <= max && Number.isFinite(sample[key]))
    .map((sample) => sample[key]));
  const lo = values.reduce((smallest, value) => Math.min(smallest, value), Infinity);
  const hi = values.reduce((largest, value) => Math.max(largest, value), -Infinity);
  const span = Math.max(hi - lo, 1);
  const rawStep = span / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const step = [1, 2, 2.5, 5, 10].find((value) => value * magnitude >= rawStep) * magnitude;
  const lower = values.length ? Math.floor(lo / step) * step : 0;
  const upper = values.length ? Math.max(lower + step, Math.ceil(hi / step) * step) : 1;
  const left = width < 400 ? 36 : 46;
  const right = width - 18;
  const top = 24;
  const bottom = height - 35;
  const x = (time) => left + (time - min) * (right - left) / Math.max(1, max - min);
  const y = (value) => bottom - (value - lower) * (bottom - top) / (upper - lower);

  ctx.setLineDash([]);
  ctx.font = '11px ui-monospace, monospace';
  ctx.fillStyle = '#626d64';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  ctx.fillText(unit, 1, 11);
  const ticks = values.length ? Math.round((upper - lower) / step) : 4;
  for (let index = 0; index <= ticks; index += 1) {
    const value = lower + index * (upper - lower) / ticks;
    const line = Math.round(y(value)) + 0.5;
    ctx.strokeStyle = '#e8eae4';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, line);
    ctx.lineTo(right, line);
    ctx.stroke();
    if (values.length) ctx.fillText(tickLabel.format(value), 1, line + 3);
  }

  // 눈금이 겹치지 않도록 1초 이상의 보기 좋은 간격을 고릅니다.
  const targetTicks = width < 420 ? 4 : 6;
  const rawTime = Math.max(1, (max - min) / targetTicks);
  const timeMagnitude = 10 ** Math.floor(Math.log10(rawTime));
  const timeStep = Math.max(1,
    ([1, 2, 5, 10].find((value) => value * timeMagnitude >= rawTime) ?? 10) * timeMagnitude);
  const firstTick = Math.ceil(min / timeStep - 1e-9) * timeStep;
  for (let time = firstTick; time <= max + 1e-6; time += timeStep) {
    ctx.textAlign = time <= min + 1e-6 ? 'left' : time >= max - 1e-6 ? 'right' : 'center';
    ctx.fillText(formatTime(time), x(time), bottom + 20);
  }
  if (!values.length) {
    ctx.textAlign = 'center';
    ctx.fillText(emptyText, (left + right) / 2, (top + bottom) / 2);
    ctx.textAlign = 'left';
    return;
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(left - 1, top - 3, right - left + 2, bottom - top + 6);
  ctx.clip();
  for (const { samples, color } of series) {
    const segments = [];
    let segment = [];
    for (const sample of samples) {
      if (sample.elapsed_s < min || sample.elapsed_s > max) continue;
      if (Number.isFinite(sample[key])) {
        segment.push([x(sample.elapsed_s), y(sample[key])]);
      } else if (segment.length) {
        segments.push(segment);
        segment = [];
      }
    }
    if (segment.length) segments.push(segment);
    for (const points of segments) {
      if (series.length === 1 && points.length > 1) {
        const gradient = ctx.createLinearGradient(0, top, 0, bottom);
        gradient.addColorStop(0, '#f4dacb');
        gradient.addColorStop(1, '#fdf7f2');
        ctx.beginPath();
        ctx.moveTo(points[0][0], bottom);
        for (const point of points) ctx.lineTo(...point);
        ctx.lineTo(points.at(-1)[0], bottom);
        ctx.fillStyle = gradient;
        ctx.fill();
      }
      ctx.beginPath();
      ctx.moveTo(...points[0]);
      for (const point of points.slice(1)) ctx.lineTo(...point);
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();
    }
    const last = segments.at(-1)?.at(-1);
    if (last && series.length === 1) {
      ctx.beginPath();
      ctx.arc(...last, 3.5, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    }
  }

  if (cursor !== undefined) {
    ctx.beginPath();
    ctx.setLineDash([4, 4]);
    ctx.moveTo(x(cursor), top);
    ctx.lineTo(x(cursor), bottom);
    ctx.strokeStyle = '#417861';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.restore();
}
