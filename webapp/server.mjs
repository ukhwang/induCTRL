import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { normalizeEntry, checkPin, readManualBody } from './manual.mjs';
import { createStore } from './store.mjs';

const targetSeconds = 750;
const fast = process.argv.includes('--fast');
let startedAt;
const files = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/chart.js', ['chart.js', 'text/javascript; charset=utf-8']],
]);

function makeSamples(duration, seed) {
  return Array.from({ length: Math.floor(duration / 2) + 1 }, (_, index) => {
    const time = index * 2;
    const conductivity = 30 + 22 * (1 - Math.exp(-time / 78)) - 4 * time / duration;
    const ripple = (Math.sin(index * 0.72 + seed) + Math.sin(index * 1.89 + seed)) * 0.85;
    return {
      elapsed_s: time,
      conductivity: time === 590 || time === 592 ? null : Number((conductivity + ripple + seed).toFixed(2)),
      rpm: Math.round(320 * (1 - Math.exp(-time / 26)) + Math.sin(index * 0.57) * 2),
      turbidity_top: Number((500 + 180 * Math.exp(-time / 65) + 5 * ripple + seed).toFixed(1)),
      turbidity_bottom: Number((500 - 130 * Math.exp(-time / 91) + 8 * ripple - seed).toFixed(1)),
    };
  });
}

function makeRun(id, label, duration, seed) {
  return {
    id, label, duration_s: duration,
    samples: makeSamples(duration, seed),
    events: [
      { elapsed_s: 0, title: '교반 시작', detail: '배치 기록을 시작했습니다.' },
      { elapsed_s: 128, title: '목표 회전수 도달', detail: '회전수가 320 RPM에 도달했습니다.' },
      { elapsed_s: 318, title: '센서 값 안정화', detail: '모의 부하가 정상 범위에 들어왔습니다.' },
      { elapsed_s: duration, title: '교반 완료', detail: '목표 교반 시간에 도달했습니다.' },
    ],
  };
}

const live = makeRun('DEMO-024', '현재 배치', targetSeconds, 0);
const recordings = [
  makeRun('DEMO-023', '모의 배치 23', 718, -0.7),
  makeRun('DEMO-022', '모의 배치 22', 774, 0.85),
  makeRun('DEMO-021', '모의 배치 21', 750, -1.45),
];

function getSnapshot() {
  // Start the mock clock with the first read, so a newly opened dashboard shows a running batch.
  startedAt ??= Date.now();
  const now = Date.now();
  const elapsed = Math.min(targetSeconds, 582 + Math.floor((now - startedAt) / 1000 * (fast ? 60 : 1)));
  const complete = elapsed === targetSeconds;
  return {
    mode: 'mock',
    received_at: new Date(now).toISOString(),
    live: {
      ...live,
      elapsed_s: elapsed,
      state: complete ? 'complete' : 'running',
      samples: live.samples.filter((sample) => sample.elapsed_s <= elapsed),
      events: live.events.filter((event) => event.elapsed_s <= elapsed),
    },
    recordings: complete ? [...recordings, live] : recordings,
  };
}

const port = Number(process.env.PORT || 3000);
const { STIR_SQLITE_DB: path, NEON_READ_URL: url, MIC_SOURCE_ID: source, NEON_WEB_WRITE_URL: writeUrl,
  WEB_ENTRY_PIN: pin, WEB_ORIGIN: origin } = process.env;
const manualOrigin = URL.parse(origin ?? '');
const validOrigin = manualOrigin && manualOrigin.origin === origin && (manualOrigin.protocol === 'https:' ||
  (manualOrigin.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(manualOrigin.hostname)));
if ((path && url) || (url && !source) || (writeUrl && (!url || !validOrigin || !/^\d{6,12}$/.test(pin ?? '')))) {
  console.error('Check STIR_SQLITE_DB, NEON_READ_URL, MIC_SOURCE_ID and optional manual entry settings.');
  process.exit(1);
}
const store = path || url ? createStore({ path, url, source, writeUrl }) : null;

function sendJSON(res, status, body, head = false) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(head ? undefined : JSON.stringify(body));
}

createServer(async (req, res) => {
  try {
    const parsed = new URL(req.url, 'http://localhost');
    const route = parsed.pathname;
    const entry = files.get(route);
    if (route === '/api/manual' && writeUrl && req.method === 'POST') {
      if (req.headers.origin !== origin) {
        sendJSON(res, 403, { error: 'origin' });
        return;
      }
      const body = await readManualBody(req);
      const record = normalizeEntry(body);
      if (!record) {
        sendJSON(res, 400, { error: 'invalid' });
        return;
      }
      const auth = checkPin(body.pin, pin);
      if (auth !== 200) {
        sendJSON(res, auth, { error: auth === 429 ? 'rate' : 'pin' });
        return;
      }
      await store.manual(record);
      sendJSON(res, 201, { id: record.id });
    } else if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.writeHead(405, { Allow: 'GET, HEAD' }).end();
    } else if (route === '/api/snapshot' || route === '/api/mock' && !store) {
      const snapshot = store ? await store.snapshot() : getSnapshot();
      sendJSON(res, 200, snapshot, req.method === 'HEAD');
    } else if (route === '/api/batch' && store) {
      const id = parsed.searchParams.get('id');
      const batch = id && id.length <= 250 ? await store.batch(id) : null;
      sendJSON(res, batch ? 200 : 404, batch, req.method === 'HEAD');
    } else if (entry) {
      const [file, contentType] = entry;
      const contents = await readFile(new URL(`./public/${file}`, import.meta.url));
      res.writeHead(200, { 'Content-Type': contentType, 'Cache-Control': 'no-cache' });
      res.end(req.method === 'HEAD' ? undefined : contents);
    } else {
      res.writeHead(404).end();
    }
  } catch (error) {
    console.error('Request failed', error.code ?? error.name);
    if (!res.headersSent) sendJSON(res, error.code === '23503' ? 404 : 503, { error: 'unavailable' });
    else res.end();
  }
}).listen(port, '0.0.0.0', () => console.log(`induCTRL ${url ? 'Neon' : path ? 'SQLite' : 'mock'} listening on port ${port}`));
