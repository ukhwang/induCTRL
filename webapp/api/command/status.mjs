/** 최신 명령 상태 + MIC 온라인 여부. 폰의 제어 패널이 2초마다 읽습니다. */
import { commandServiceFor, neonEnv, send } from '../../apilib.mjs';

export default async function handler(req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    return send(res, 405, { error: 'method' }, { Allow: 'GET, HEAD' });
  }
  const env = neonEnv();
  if (!env.commandUrl) {
    return send(res, 200, {
      mode: 'sim_only',
      pin_required: false,
      mic: null,
      active: null,
      stop: null,
      recent: [],
      batch: null,
      running: false,
      server_time: new Date().toISOString(),
    });
  }
  try {
    const state = await commandServiceFor(env).status();
    return send(res, 200, state);
  } catch (error) {
    console.error('command status failed', error?.code ?? error?.name);
    return send(res, 200, {
      mode: 'neon',
      degraded: true,
      pin_required: Boolean(env.pin),
      mic: null,
      active: null,
      stop: null,
      recent: [],
      batch: null,
      running: false,
      server_time: new Date().toISOString(),
      message: 'DB 응답을 받지 못했습니다.',
    });
  }
}
