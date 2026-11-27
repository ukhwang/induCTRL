/** 교반 시작/정지 명령을 Neon 큐에 넣습니다. 202 = 접수, 409 = 이미 운전 중. */
import { clientKey, commandServiceFor, neonEnv, readJson, send } from '../../apilib.mjs';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return send(res, 405, { ok: false, error: 'method' }, { Allow: 'POST' });
  }
  const env = neonEnv();
  if (!env.commandUrl) {
    return send(res, 503, {
      ok: false,
      error: 'control_disabled',
      message: '이 배포에는 DB 가 연결되지 않았습니다. 위쪽 시뮬레이터를 사용하세요.',
    });
  }
  const body = await readJson(req);
  if (!body) return send(res, 400, { ok: false, error: 'invalid', message: 'JSON 본문이 필요합니다.' });
  try {
    const service = commandServiceFor(env);
    const result = await service.send(body, {
      pin: req.headers['x-exhibition-pin'] ?? body.pin,
      client: clientKey(req),
    });
    return send(res, result.status, result.body);
  } catch (error) {
    console.error('command failed', error?.code ?? error?.name);
    return send(res, 503, { ok: false, error: 'unavailable', message: 'DB 응답을 받지 못했습니다.' });
  }
}
