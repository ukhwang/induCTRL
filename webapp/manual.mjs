import { createHash, timingSafeEqual } from 'node:crypto';

const failures = [];
const hash = (value) => createHash('sha256').update(value).digest();

function text(value, max) {
  if (typeof value !== 'string') return null;
  return value.trim().slice(0, max) || null;
}

export function normalizeEntry(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const { batchId, id, kind } = entry;
  if (typeof batchId !== 'string' || !batchId.trim() || batchId.length > 250) return null;
  if (typeof id !== 'string' || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id)) return null;
  const output = {
    batchId, id, kind, metric: null, position: null, value: null, unit: null, passed: null,
    note: text(entry.note, 500), measuredAt: null,
  };
  if (entry.measuredAt) {
    if (typeof entry.measuredAt !== 'string' || !Number.isFinite(Date.parse(entry.measuredAt))) return null;
    output.measuredAt = new Date(entry.measuredAt).toISOString();
  }
  if (kind === 'sample') {
    output.metric = text(entry.metric, 60);
    output.position = text(entry.position, 40);
    output.unit = text(entry.unit, 30);
    output.value = entry.value;
    if (!output.metric || !Number.isFinite(output.value)) return null;
  } else if (kind === 'quality') {
    if (typeof entry.passed !== 'boolean') return null;
    output.passed = Number(entry.passed);
  } else if (kind !== 'manual_stop') {
    return null;
  }
  return output;
}

export function checkPin(pin, expected, now = Date.now()) {
  while (failures.length && failures[0] <= now - 300_000) failures.shift();
  if (failures.length >= 5) return 429;
  if (typeof pin !== 'string' || !timingSafeEqual(hash(pin), hash(expected))) {
    failures.push(now);
    return 403;
  }
  return 200;
}

export async function readManualBody(request) {
  if (!request.headers['content-type']?.toLowerCase().startsWith('application/json')) return null;
  const parts = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 4096) return null;
    parts.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(parts).toString());
  } catch {
    return null;
  }
}
