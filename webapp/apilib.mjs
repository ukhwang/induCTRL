/**
 * Vercel 서버리스 함수(webapp/api/*)가 같이 쓰는 도우미.
 * 함수 폴더(api/) 밖에 두어 Vercel 이 이 파일을 엔드포인트로 만들지 않게 합니다.
 */
import { createStore } from './store.mjs';
import { createCommandService, DEFAULT_SOURCE_ID } from './commands.mjs';

export function neonEnv() {
  const readUrl = process.env.DATABASE_URL || process.env.NEON_READ_URL || '';
  const commandUrl = process.env.NEON_COMMAND_URL || readUrl;
  const source = process.env.MIC_SOURCE_ID || DEFAULT_SOURCE_ID;
  const pin = process.env.EXHIBITION_PIN || '';
  const online = Number(process.env.MIC_ONLINE_SECONDS || 0);
  return { readUrl, commandUrl, source, pin, online };
}

export function send(res, status, body, headers = {}) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  for (const [key, value] of Object.entries(headers)) res.setHeader(key, value);
  res.end(JSON.stringify(body));
}

/** 4KB 넘는 본문은 받지 않습니다. Vercel 이 미리 파싱한 req.body 도 받습니다. */
export async function readJson(req, limit = 4096) {
  if (req.body !== undefined && req.body !== null) {
    if (typeof req.body === 'object') return req.body;
    if (typeof req.body === 'string') {
      if (req.body.length > limit) return null;
      try {
        return JSON.parse(req.body);
      } catch {
        return null;
      }
    }
    return null;
  }
  if (!String(req.headers['content-type'] ?? '').toLowerCase().startsWith('application/json')) return null;
  const parts = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) return null;
    parts.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(parts).toString('utf8'));
  } catch {
    return null;
  }
}

export function clientKey(req) {
  const forwarded = String(req.headers['x-forwarded-for'] ?? '').split(',')[0].trim();
  return forwarded || req.socket?.remoteAddress || 'unknown';
}

// 함수 인스턴스(웜 컨테이너)마다 커넥션을 재사용합니다. 유휴 8초 뒤 닫아
// Neon 무료 컴퓨트가 쉬도록 합니다.
let storeCache = null;
let serviceCache = null;

export function storeFor(readUrl, source) {
  const key = `${readUrl}\u0000${source}`;
  if (!storeCache || storeCache.key !== key) {
    storeCache = {
      key,
      store: createStore({ url: readUrl, source, poolOptions: { max: 1 } }),
    };
  }
  return storeCache.store;
}

export function commandServiceFor({ commandUrl, source, pin, online }) {
  const key = `${commandUrl}\u0000${source}\u0000${pin}\u0000${online}`;
  if (!serviceCache || serviceCache.key !== key) {
    serviceCache = {
      key,
      service: createCommandService({
        url: commandUrl,
        source,
        pin,
        micOnlineSeconds: Number.isFinite(online) && online > 0 ? online : undefined,
        pool: undefined,
      }),
    };
  }
  return serviceCache.service;
}

/** /api/snapshot 과 /api/live 가 같이 쓰는 조회 응답. DB 가 없으면 시뮬레이터 폴백 신호. */
export async function snapshotResponse() {
  const { readUrl, source } = neonEnv();
  if (!readUrl) {
    return {
      status: 200,
      body: {
        mode: 'sim_only',
        live: null,
        recordings: [],
        received_at: null,
        manual_enabled: false,
        control_enabled: false,
      },
    };
  }
  try {
    const snapshot = await storeFor(readUrl, source).snapshot();
    return { status: 200, body: { ...snapshot, control_enabled: true } };
  } catch (error) {
    console.error('snapshot failed', error?.code ?? error?.name);
    return {
      status: 200,
      body: { mode: 'sim_only', live: null, recordings: [], received_at: null, manual_enabled: false, control_enabled: false, degraded: true },
    };
  }
}
