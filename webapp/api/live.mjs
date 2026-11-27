/** 현재 배치·센서·이벤트 조회. 앱은 /api/snapshot 을 부르고, 새 코드는 /api/live 도 같은 응답을 받습니다. */
import { send, snapshotResponse } from '../apilib.mjs';

export default async function handler(req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    return send(res, 405, { error: 'method' }, { Allow: 'GET, HEAD' });
  }
  const { status, body } = await snapshotResponse();
  return send(res, status, body);
}
