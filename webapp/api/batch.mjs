/** 과거 배치 하나의 전체 기록(비교·재생 화면용). */
import { neonEnv, send, storeFor } from '../apilib.mjs';

export default async function handler(req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    return send(res, 405, { error: 'method' }, { Allow: 'GET, HEAD' });
  }
  const { readUrl, source } = neonEnv();
  if (!readUrl) return send(res, 404, { error: 'no_database' });
  const id = String(req.query?.id ?? '');
  if (!id || id.length > 250) return send(res, 400, { error: 'invalid' });
  try {
    const batch = await storeFor(readUrl, source).batch(id);
    return batch ? send(res, 200, batch) : send(res, 404, { error: 'not_found' });
  } catch (error) {
    console.error('batch failed', error?.code ?? error?.name);
    return send(res, 503, { error: 'unavailable' });
  }
}
