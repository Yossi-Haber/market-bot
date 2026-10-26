interface D1Result { success?: boolean; meta?: { changes?: number }; }
interface D1Statement { bind(...values: unknown[]): D1Statement; first<T = Record<string, unknown>>(): Promise<T | null>; all<T = Record<string, unknown>>(): Promise<{ results: T[] }>; run(): Promise<D1Result>; }
interface D1Database { prepare(query: string): D1Statement; batch<T = D1Result>(statements: D1Statement[]): Promise<T[]>; }
interface Env {
  DB: D1Database;
  STATE_API_HMAC_SECRET: string;
  STATE_ADMIN_HMAC_SECRET: string;
  MARKETBOT_TEST_MODE?: string;
  MARKETBOT_TEST_DATABASE_ID?: string;
  MARKETBOT_TEST_WORKER_NAME?: string;
}

const SCHEMA_VERSION = 1;
const MAX_BODY_BYTES = 16 * 1024;
const MAX_CLOCK_SKEW_SECONDS = 300;
const MINUTE_LIMIT = 8;
const DAILY_LIMIT = 800;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const ID_RE = /^[0-9a-f]{32}$/;
const SLOT_RE = /^hmac256:[0-9a-f]{64}$/;
const SAFE_CODE_RE = /^[A-Z0-9_]{1,80}$/;
const TEST_DATABASE_ID = "8049a22c-eb1f-4e9d-928d-29b35dcc6f70";
const TEST_WORKER_NAME = "marketbot-state-api-test";
const TEST_MODE = "phase3c-synthetic-mutations-v1";
const TEST_ID_RE = /^3c[0-9a-f]{30}$/;
const TEST_RECIPIENT_SLOTS = new Set([
  "hmac256:ac00581af76fae2942fefc401985c751c927c4412b3e8486f54cda8e4d420419",
  "hmac256:ad4e4df2c02723b74f014769278cd2c1edac43e7f17751f1b9048f40b11aa692",
]);
const TEST_REVIEWER_SLOT = "hmac256:ad4e4df2c02723b74f014769278cd2c1edac43e7f17751f1b9048f40b11aa692";
const TEST_DATE_RE = /^209[0-9]-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$/;

type JsonObject = Record<string, unknown>;
type Clock = () => Date;

class ApiError extends Error {
  constructor(readonly status: number, readonly code: string) { super(code); }
}

export async function handleRequest(request: Request, env: Env, clock: Clock = () => new Date()): Promise<Response> {
  if (request.method !== "POST" || new URL(request.url).pathname !== "/v1/state") return json({ error: "NOT_FOUND" }, 404);
  const contentType = request.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
  if (contentType !== "application/json") return json({ error: "CONTENT_TYPE_INVALID" }, 415);
  const declaredLength = Number(request.headers.get("content-length") || "0");
  if (declaredLength > MAX_BODY_BYTES) return json({ error: "REQUEST_TOO_LARGE" }, 413);
  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) return json({ error: "REQUEST_TOO_LARGE" }, 413);
  let body: JsonObject;
  try { body = JSON.parse(raw) as JsonObject; } catch { return json({ error: "JSON_INVALID" }, 400); }
  if (!body || typeof body !== "object" || Array.isArray(body) || body.schema_version !== SCHEMA_VERSION || typeof body.operation_id !== "string" || !ID_RE.test(body.operation_id) || typeof body.op !== "string" || !body.payload || typeof body.payload !== "object" || Array.isArray(body.payload)) {
    return json({ error: "REQUEST_SCHEMA_INVALID" }, 400);
  }
  if (Object.keys(body).some(key => !["schema_version", "operation_id", "op", "payload"].includes(key))) return json({ error: "REQUEST_SCHEMA_INVALID" }, 400);
  const adminOps = new Set(["ownership.set", "admin.prune", "delivery.reconcile"]);
  try {
    await authenticate(request, raw, body, env, clock, adminOps.has(body.op));
    await requireSchema(env.DB);
    const testMode = await resolveTestMutationMode(env);
    const payload = body.payload as JsonObject;
    if (testMode && body.op !== "state.get") validateSyntheticTestRequest(body.op, body.operation_id as string, payload);
    const result = await dispatch(env.DB, body.operation_id, body.op, payload, clock(), testMode);
    if (testMode && body.op === "state.get") result.body.test_mutation_mode = true;
    return json(result.body, result.status);
  } catch (error) {
    if (error instanceof ApiError) return json({ error: error.code }, error.status);
    // Provider/D1/SQL details and request contents are intentionally not logged.
    const errorClass = error instanceof Error ? error.name.replace(/[^A-Za-z0-9]/g, "").slice(0, 40) : "UnknownError";
    console.error(`STATE_BACKEND_FAILURE:${errorClass}`);
    return json({ error: "STATE_BACKEND_UNAVAILABLE" }, 503);
  }
}

async function authenticate(request: Request, raw: string, body: JsonObject, env: Env, clock: Clock, admin: boolean): Promise<void> {
  const tsText = request.headers.get("x-mb-timestamp") || "";
  const nonce = request.headers.get("x-mb-nonce") || "";
  const signature = request.headers.get(admin ? "x-mb-admin-signature" : "x-mb-signature") || "";
  const ts = Number(tsText);
  if (!Number.isSafeInteger(ts) || Math.abs(Math.floor(clock().getTime() / 1000) - ts) > MAX_CLOCK_SKEW_SECONDS || !/^[0-9a-f]{32}$/.test(nonce) || !/^[0-9a-f]{64}$/.test(signature)) throw new ApiError(401, "AUTHENTICATION_FAILED");
  const secret = admin ? env.STATE_ADMIN_HMAC_SECRET : env.STATE_API_HMAC_SECRET;
  if (!secret || secret.length < 32) throw new ApiError(503, "STATE_AUTH_NOT_CONFIGURED");
  const expected = await hmacHex(secret, `${tsText}.${nonce}.${raw}`);
  if (!constantTimeEqual(signature, expected)) throw new ApiError(401, "AUTHENTICATION_FAILED");
  try {
    await env.DB.batch([
      env.DB.prepare("DELETE FROM request_nonces WHERE created_at < ?").bind(ts - 600),
      env.DB.prepare("INSERT INTO request_nonces(nonce, created_at) VALUES (?, ?)").bind(nonce, ts),
    ]);
  } catch { throw new ApiError(401, "AUTHENTICATION_FAILED"); }
  if (admin && !new Set(["ownership.set", "admin.prune", "delivery.reconcile"]).has(String(body.op))) throw new ApiError(403, "ADMIN_OPERATION_NOT_ALLOWED");
}

async function requireSchema(db: D1Database): Promise<void> {
  const row = await db.prepare("SELECT version FROM schema_meta WHERE singleton=1").first<{ version: number }>();
  if (!row || row.version !== SCHEMA_VERSION) throw new ApiError(503, "SCHEMA_VERSION_MISMATCH");
}

interface DispatchResult { status: number; body: JsonObject; }
async function dispatch(db: D1Database, opId: string, op: string, payload: JsonObject, now: Date, testMode = false): Promise<DispatchResult> {
  validatePayloadKeys(op, payload);
  if (testMode && ["ownership.set", "admin.prune"].includes(op)) throw new ApiError(403, "TEST_ADMIN_OPERATION_FORBIDDEN");
  switch (op) {
    case "state.get": return stateGet(db, payload);
    case "quota.reserve": return quotaReserve(db, opId, payload, now, testMode);
    case "run.claim": return runClaim(db, opId, payload, now, testMode);
    case "run.transition": return runTransition(db, opId, payload, now);
    case "run.recover": return runRecover(db, opId, payload, now);
    case "delivery.begin": return deliveryBegin(db, opId, payload, now, testMode);
    case "delivery.outcome": return deliveryOutcome(db, opId, payload, now);
    case "delivery.reconcile": return deliveryReconcile(db, opId, payload, now);
    case "ownership.set": return ownershipSet(db, opId, payload, now);
    case "admin.prune": return prune(db, opId, payload, now);
    default: throw new ApiError(400, "OPERATION_NOT_ALLOWED");
  }
}

async function stateGet(db: D1Database, payload: JsonObject): Promise<DispatchResult> {
  const marketDate = payload.market_date;
  if (marketDate !== undefined && !isDate(marketDate)) throw new ApiError(400, "MARKET_DATE_INVALID");
  const [ownership, stateMeta, runs, run, quotaBuckets, quotaSummary, deliverySummary, operationSummary, integrity] = await Promise.all([
    db.prepare("SELECT cloud_quota_exclusive, windows_sender_disabled, cloud_delivery_enabled FROM ownership WHERE singleton=1").first<JsonObject>(),
    db.prepare("SELECT generation,created_at FROM state_meta WHERE singleton=1").first<{ generation: number; created_at: string }>(),
    db.prepare("SELECT market_date, run_id, status, claimed_at, started_at, finished_at, reason_code FROM run_claim ORDER BY market_date").all<JsonObject>(),
    marketDate ? db.prepare("SELECT market_date, run_id, status, claimed_at, started_at, finished_at, reason_code FROM run_claim WHERE market_date=?").bind(marketDate).first<JsonObject>() : Promise.resolve(null),
    db.prepare("SELECT period,bucket_key,used_credits,limit_credits FROM quota_bucket ORDER BY period,bucket_key").all<JsonObject>(),
    db.prepare("SELECT utc_day,COUNT(*) AS reservation_count,SUM(credits) AS credits FROM quota_reservation GROUP BY utc_day ORDER BY utc_day").all<JsonObject>(),
    db.prepare("SELECT market_date,status,COUNT(*) AS recipient_count,SUM(attempt) AS attempts_total,MAX(attempt) AS max_attempt FROM delivery_recipient GROUP BY market_date,status ORDER BY market_date,status").all<JsonObject>(),
    db.prepare("SELECT operation_type,status,COUNT(*) AS operation_count FROM operations GROUP BY operation_type,status ORDER BY operation_type,status").all<JsonObject>(),
    db.prepare(`SELECT
      (SELECT COUNT(*) FROM quota_reservation q LEFT JOIN operations o ON o.operation_id=q.reservation_id AND o.operation_type='quota.reserve' AND o.status='COMMITTED' WHERE o.operation_id IS NULL) AS reservations_without_operation,
      (SELECT COUNT(*) FROM run_claim r LEFT JOIN operations o ON o.operation_id=r.claim_operation_id AND o.operation_type='run.claim' AND o.status='COMMITTED' WHERE o.operation_id IS NULL AND r.claimed_at>=strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days')) AS recent_runs_without_claim_operation,
      (SELECT COUNT(*) FROM run_claim r LEFT JOIN operations o ON o.operation_id=r.last_operation_id AND o.status='COMMITTED' WHERE o.operation_id IS NULL AND r.claimed_at>=strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days')) AS recent_runs_without_last_operation,
      (SELECT COUNT(*) FROM delivery_recipient d LEFT JOIN operations o ON o.operation_id=d.last_operation_id AND o.status='COMMITTED' WHERE o.operation_id IS NULL AND d.updated_at>=strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days')) AS recent_deliveries_without_last_operation,
      (SELECT COUNT(*) FROM delivery_recipient d LEFT JOIN run_claim r ON r.market_date=d.market_date WHERE r.market_date IS NULL) AS deliveries_without_run,
      (SELECT COUNT(*) FROM run_claim r JOIN delivery_recipient d ON d.market_date=r.market_date WHERE r.status='COMPLETE' AND d.status<>'SENT') AS completed_runs_with_unsent_delivery,
      (SELECT COUNT(*) FROM quota_bucket b WHERE b.period='day' AND b.used_credits<>(SELECT COALESCE(SUM(q.credits),0) FROM quota_reservation q WHERE q.utc_day=b.bucket_key)) AS daily_quota_bucket_mismatches,
      (SELECT COUNT(*) FROM quota_bucket b WHERE b.period='minute' AND b.used_credits<>(SELECT COALESCE(SUM(q.credits),0) FROM quota_reservation q WHERE q.utc_minute=b.bucket_key)) AS minute_quota_bucket_mismatches`).first<JsonObject>(),
  ]);
  if (!ownership || !stateMeta) throw new ApiError(503, "OWNERSHIP_STATE_MISSING");
  const toBool = (v: unknown) => Number(v) === 1;
  const runsByDate = Object.fromEntries(runs.results.map(item => [String(item.market_date), item]));
  return { status: 200, body: {
    schema_version: SCHEMA_VERSION,
    generation: stateMeta.generation,
    created_at: stateMeta.created_at,
    ownership: { cloud_quota_exclusive: toBool(ownership.cloud_quota_exclusive), windows_sender_disabled: toBool(ownership.windows_sender_disabled), cloud_delivery_enabled: toBool(ownership.cloud_delivery_enabled) },
    runs: runsByDate,
    ...(marketDate ? { run } : {}),
    quota: { buckets: quotaBuckets.results, reservations_by_utc_day: quotaSummary.results },
    deliveries_by_market_date_and_status: deliverySummary.results,
    operations_by_type_and_status: operationSummary.results,
    integrity_window_days: 7,
    integrity: integrity || {},
  } };
}

async function quotaReserve(db: D1Database, opId: string, p: JsonObject, now: Date, testMode = false): Promise<DispatchResult> {
  if (p.provider !== "twelve_data" || !Number.isInteger(p.credits) || Number(p.credits) < 1 || Number(p.credits) > MINUTE_LIMIT) throw new ApiError(400, "QUOTA_REQUEST_INVALID");
  const utc = now.toISOString(); const day = utc.slice(0, 10); const minute = `${utc.slice(0, 16)}Z`; const credits = Number(p.credits);
  const hash = await requestHash("quota.reserve", { provider: p.provider, credits });
  const replay = await priorOperation(db, opId, "quota.reserve", hash);
  if (replay) return { status: 200, body: { ...replay, authorized: false, replayed: true } };
  await requireOwnership(db, false, testMode);
  try {
    const results = await db.batch([
      operationInsert(db, opId, "quota.reserve", hash, now),
      db.prepare("INSERT INTO quota_bucket(period,bucket_key,used_credits,limit_credits) SELECT 'day',?,?,? WHERE changes()=1 ON CONFLICT(period,bucket_key) DO UPDATE SET used_credits=quota_bucket.used_credits+excluded.used_credits").bind(day, credits, DAILY_LIMIT),
      db.prepare("INSERT INTO quota_bucket(period,bucket_key,used_credits,limit_credits) SELECT 'minute',?,?,? WHERE changes()=1 ON CONFLICT(period,bucket_key) DO UPDATE SET used_credits=quota_bucket.used_credits+excluded.used_credits").bind(minute, credits, MINUTE_LIMIT),
      db.prepare("INSERT INTO quota_reservation(reservation_id,provider,utc_day,utc_minute,credits,created_at) SELECT ?, 'twelve_data',?,?,?,? WHERE changes()=1").bind(opId, day, minute, credits, utc),
      operationCommit(db, opId),
    ]);
    if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "quota.reserve", hash);
  } catch (error) {
    const bucket = await db.prepare("SELECT used_credits,limit_credits FROM quota_bucket WHERE (period='day' AND bucket_key=?) OR (period='minute' AND bucket_key=?)").bind(day, minute).all<{ used_credits: number; limit_credits: number }>();
    if (bucket.results.some(row => row.used_credits + credits > row.limit_credits)) throw new ApiError(429, "QUOTA_EXHAUSTED");
    throw error;
  }
  const reserved = await db.prepare("SELECT 1 FROM quota_reservation WHERE reservation_id=?").bind(opId).first();
  if (!reserved) throw new ApiError(429, "QUOTA_EXHAUSTED");
  return { status: 200, body: { operation_id: opId, reservation_id: opId, authorized: true, replayed: false } };
}

async function runClaim(db: D1Database, opId: string, p: JsonObject, now: Date, testMode = false): Promise<DispatchResult> {
  const date = requireDate(p.market_date); const runId = requireId(p.run_id, "RUN_ID_INVALID");
  const hash = await requestHash("run.claim", { market_date: date, run_id: runId });
  const replay = await priorOperation(db, opId, "run.claim", hash);
  if (replay) return { status: 200, body: { ...replay, claim_authorized: false, replayed: true } };
  await requireOwnership(db, false, testMode);
  try {
    const ts = now.toISOString();
    const results = await db.batch([
      operationInsert(db, opId, "run.claim", hash, now),
      db.prepare("INSERT INTO run_claim(market_date,run_id,status,claim_operation_id,last_operation_id,claimed_at) SELECT ?,?,'CLAIMED',?,?,? WHERE changes()=1").bind(date, runId, opId, opId, ts),
      operationCommit(db, opId),
    ]);
    if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "run.claim", hash);
  } catch {
    const existing = await db.prepare("SELECT 1 FROM run_claim WHERE market_date=? OR run_id=?").bind(date, runId).first();
    if (existing) throw new ApiError(409, "MARKET_DATE_ALREADY_CLAIMED");
    throw new ApiError(503, "STATE_BACKEND_UNAVAILABLE");
  }
  return { status: 200, body: { operation_id: opId, market_date: date, run_id: runId, claim_authorized: true, replayed: false } };
}

async function runTransition(db: D1Database, opId: string, p: JsonObject, now: Date): Promise<DispatchResult> {
  const date = requireDate(p.market_date); const runId = requireId(p.run_id, "RUN_ID_INVALID");
  const from = requireEnum(p.from, ["CLAIMED","RUNNING","REPORT_READY","DELIVERY_IN_PROGRESS"]);
  const to = requireEnum(p.to, ["RUNNING","REPORT_READY","DELIVERY_IN_PROGRESS","COMPLETE","FAILED","PARTIAL_DELIVERY","DELIVERY_UNKNOWN"]);
  if (!validRunTransition(from, to)) throw new ApiError(400, "RUN_TRANSITION_INVALID");
  const reason = p.reason_code === undefined ? null : requireSafeCode(p.reason_code);
  const hash = await requestHash("run.transition", { market_date: date, run_id: runId, from, to, reason_code: reason });
  const replay = await priorOperation(db, opId, "run.transition", hash);
  if (replay) return { status: replay.applied ? 200 : 409, body: { ...replay, replayed: true } };
  const ts = now.toISOString();
  const results = await db.batch([
    operationInsert(db, opId, "run.transition", hash, now),
    db.prepare("UPDATE run_claim SET status=?,last_operation_id=?,started_at=CASE WHEN ?='RUNNING' AND started_at IS NULL THEN ? ELSE started_at END,finished_at=CASE WHEN ? IN ('COMPLETE','FAILED','PARTIAL_DELIVERY','DELIVERY_UNKNOWN') THEN ? ELSE finished_at END,reason_code=? WHERE market_date=? AND run_id=? AND status=? AND changes()=1").bind(to, opId, to, ts, to, ts, reason, date, runId, from),
    operationCommit(db, opId),
  ]);
  if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "run.transition", hash);
  const row = await db.prepare("SELECT last_operation_id FROM run_claim WHERE market_date=? AND run_id=?").bind(date, runId).first<{ last_operation_id: string }>();
  const applied = row?.last_operation_id === opId;
  await storeOperationResult(db, opId, { operation_id: opId, applied });
  return { status: applied ? 200 : 409, body: { operation_id: opId, applied, replayed: false } };
}

async function runRecover(db: D1Database, opId: string, p: JsonObject, now: Date): Promise<DispatchResult> {
  const date = requireDate(p.market_date); const runId = requireId(p.run_id, "RUN_ID_INVALID");
  const hash = await requestHash("run.recover", { market_date: date, run_id: runId });
  const replay = await priorOperation(db, opId, "run.recover", hash);
  if (replay) return { status: 200, body: { ...replay, replayed: true } };
  const ts = now.toISOString();
  const results = await db.batch([
    operationInsert(db, opId, "run.recover", hash, now),
    db.prepare("UPDATE delivery_recipient SET status='UNKNOWN',safe_error='RECOVERED_IN_FLIGHT_AFTER_RESTART',updated_at=?,last_operation_id=? WHERE market_date=? AND status='SENDING' AND changes()=1").bind(ts, opId, date),
    db.prepare("UPDATE run_claim SET status=CASE WHEN EXISTS(SELECT 1 FROM delivery_recipient WHERE market_date=? AND status='UNKNOWN') THEN 'DELIVERY_UNKNOWN' ELSE 'FAILED' END,reason_code=CASE WHEN EXISTS(SELECT 1 FROM delivery_recipient WHERE market_date=? AND status='UNKNOWN') THEN 'INTERRUPTED_SEND_REQUIRES_REVIEW' ELSE 'INTERRUPTED_RUN_REQUIRES_REVIEW' END,finished_at=?,last_operation_id=? WHERE market_date=? AND run_id=? AND status='RUNNING'").bind(date, date, ts, opId, date, runId),
    operationCommit(db, opId),
  ]);
  if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "run.recover", hash);
  const row = await db.prepare("SELECT last_operation_id,status FROM run_claim WHERE market_date=? AND run_id=?").bind(date, runId).first<{ last_operation_id: string; status: string }>();
  const applied = row?.last_operation_id === opId;
  await storeOperationResult(db, opId, { operation_id: opId, applied, status: row?.status || "MISSING" });
  return { status: applied ? 200 : 409, body: { operation_id: opId, applied, status: row?.status || "MISSING", replayed: false } };
}

async function deliveryBegin(db: D1Database, opId: string, p: JsonObject, now: Date, testMode = false): Promise<DispatchResult> {
  const date = requireDate(p.market_date); const slot = requireSlot(p.recipient_hmac); const attemptId = requireId(p.attempt_id, "ATTEMPT_ID_INVALID");
  const reviewed = p.reviewed_retry === true;
  const hash = await requestHash("delivery.begin", { market_date: date, recipient_hmac: slot, attempt_id: attemptId, reviewed_retry: reviewed });
  const replay = await priorOperation(db, opId, "delivery.begin", hash);
  if (replay) return { status: 200, body: { ...replay, authorized: false, replayed: true } };
  await requireOwnership(db, true, testMode);
  const ts = now.toISOString();
  try {
    const results = await db.batch([
      operationInsert(db, opId, "delivery.begin", hash, now),
      db.prepare("INSERT INTO delivery_recipient(market_date,recipient_hmac,status,attempt,attempt_id,last_operation_id,send_started_at,updated_at) SELECT ?,?,'SENDING',1,?,?,?,? WHERE changes()=1 ON CONFLICT(market_date,recipient_hmac) DO UPDATE SET status='SENDING',attempt=delivery_recipient.attempt+1,attempt_id=excluded.attempt_id,last_operation_id=excluded.last_operation_id,send_started_at=excluded.send_started_at,updated_at=excluded.updated_at,safe_error=NULL WHERE delivery_recipient.status='FAILED_SAFE' AND ?=1").bind(date, slot, attemptId, opId, ts, ts, reviewed ? 1 : 0),
      operationCommit(db, opId),
    ]);
    if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "delivery.begin", hash);
  } catch {
    const existing = await db.prepare("SELECT status FROM delivery_recipient WHERE market_date=? AND recipient_hmac=?").bind(date, slot).first<{ status: string }>();
    if (existing && !(reviewed && existing.status === "FAILED_SAFE")) throw new ApiError(409, "DELIVERY_SLOT_NOT_SENDABLE");
    throw new ApiError(503, "STATE_BACKEND_UNAVAILABLE");
  }
  const row = await db.prepare("SELECT last_operation_id,attempt FROM delivery_recipient WHERE market_date=? AND recipient_hmac=?").bind(date, slot).first<{ last_operation_id: string; attempt: number }>();
  const authorized = row?.last_operation_id === opId;
  await storeOperationResult(db, opId, { operation_id: opId, authorized, attempt: row?.attempt || 0 });
  return { status: authorized ? 200 : 409, body: { operation_id: opId, authorized, attempt: row?.attempt || 0, replayed: false } };
}

async function deliveryOutcome(db: D1Database, opId: string, p: JsonObject, now: Date): Promise<DispatchResult> {
  const date = requireDate(p.market_date); const slot = requireSlot(p.recipient_hmac);
  const to = requireEnum(p.status, ["SENT","UNKNOWN","FAILED_SAFE"]); const code = p.safe_error === undefined ? null : requireSafeCode(p.safe_error);
  const hash = await requestHash("delivery.outcome", { market_date: date, recipient_hmac: slot, status: to, safe_error: code });
  const replay = await priorOperation(db, opId, "delivery.outcome", hash);
  if (replay) return { status: replay.applied ? 200 : 409, body: { ...replay, replayed: true } };
  const results = await db.batch([
    operationInsert(db, opId, "delivery.outcome", hash, now),
    db.prepare("UPDATE delivery_recipient SET status=?,safe_error=?,updated_at=?,last_operation_id=? WHERE market_date=? AND recipient_hmac=? AND status='SENDING' AND changes()=1").bind(to, code, now.toISOString(), opId, date, slot),
    operationCommit(db, opId),
  ]);
  if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "delivery.outcome", hash);
  const row = await db.prepare("SELECT last_operation_id FROM delivery_recipient WHERE market_date=? AND recipient_hmac=?").bind(date, slot).first<{ last_operation_id: string }>();
  const applied = row?.last_operation_id === opId;
  await storeOperationResult(db, opId, { operation_id: opId, applied });
  return { status: applied ? 200 : 409, body: { operation_id: opId, applied, replayed: false } };
}

async function deliveryReconcile(db: D1Database, opId: string, p: JsonObject, now: Date): Promise<DispatchResult> {
  const date = requireDate(p.market_date); const slot = requireSlot(p.recipient_hmac); const reviewer = requireSlot(p.reviewer_hmac);
  const confirmed = p.confirmed_sent === true;
  const hash = await requestHash("delivery.reconcile", { market_date: date, recipient_hmac: slot, reviewer_hmac: reviewer, confirmed_sent: confirmed });
  const replay = await priorOperation(db, opId, "delivery.reconcile", hash);
  if (replay) return { status: replay.applied ? 200 : 409, body: { ...replay, replayed: true } };
  const status = confirmed ? "SENT" : "FAILED_SAFE"; const code = confirmed ? "OPERATOR_CONFIRMED_SENT" : "OPERATOR_CONFIRMED_NOT_SENT";
  const results = await db.batch([
    operationInsert(db, opId, "delivery.reconcile", hash, now),
    db.prepare("UPDATE delivery_recipient SET status=?,reviewer_hmac=?,reconciliation_code=?,updated_at=?,last_operation_id=? WHERE market_date=? AND recipient_hmac=? AND status='UNKNOWN' AND changes()=1").bind(status, reviewer, code, now.toISOString(), opId, date, slot),
    operationCommit(db, opId),
    db.prepare("INSERT INTO audit_event(audit_id,event_type,subject_ref,actor_ref,created_at) SELECT ?,?,?,?,? WHERE EXISTS(SELECT 1 FROM delivery_recipient WHERE last_operation_id=?)").bind(opId, code, `${date}:${slot}`, reviewer, now.toISOString(), opId),
  ]);
  if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "delivery.reconcile", hash);
  const row = await db.prepare("SELECT last_operation_id FROM delivery_recipient WHERE market_date=? AND recipient_hmac=?").bind(date, slot).first<{ last_operation_id: string }>();
  const applied = row?.last_operation_id === opId;
  await storeOperationResult(db, opId, { operation_id: opId, applied, status: applied ? status : "UNKNOWN" });
  return { status: applied ? 200 : 409, body: { operation_id: opId, applied, status: applied ? status : "UNKNOWN", replayed: false } };
}

async function ownershipSet(db: D1Database, opId: string, p: JsonObject, now: Date): Promise<DispatchResult> {
  for (const key of ["cloud_quota_exclusive","windows_sender_disabled","cloud_delivery_enabled"]) if (typeof p[key] !== "boolean") throw new ApiError(400, "OWNERSHIP_STATE_INVALID");
  if (p.cloud_delivery_enabled === true && (p.cloud_quota_exclusive !== true || p.windows_sender_disabled !== true)) throw new ApiError(409, "OWNERSHIP_PREREQUISITES_REQUIRED");
  const values = { cloud_quota_exclusive: p.cloud_quota_exclusive, windows_sender_disabled: p.windows_sender_disabled, cloud_delivery_enabled: p.cloud_delivery_enabled };
  const hash = await requestHash("ownership.set", values);
  const replay = await priorOperation(db, opId, "ownership.set", hash);
  if (replay) return { status: 200, body: { ...replay, replayed: true } };
  const results = await db.batch([
    operationInsert(db, opId, "ownership.set", hash, now),
    db.prepare("UPDATE ownership SET cloud_quota_exclusive=?,windows_sender_disabled=?,cloud_delivery_enabled=?,updated_at=? WHERE singleton=1 AND changes()=1").bind(values.cloud_quota_exclusive ? 1 : 0, values.windows_sender_disabled ? 1 : 0, values.cloud_delivery_enabled ? 1 : 0, now.toISOString()),
    operationCommit(db, opId),
    db.prepare("INSERT INTO audit_event(audit_id,event_type,subject_ref,actor_ref,created_at) VALUES (?, 'OWNERSHIP_UPDATED', 'GLOBAL', 'ADMIN_HMAC', ?)").bind(opId, now.toISOString()),
  ]);
  if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "ownership.set", hash);
  await storeOperationResult(db, opId, { operation_id: opId, applied: true });
  return { status: 200, body: { operation_id: opId, applied: true, replayed: false } };
}

async function prune(db: D1Database, opId: string, p: JsonObject, now: Date): Promise<DispatchResult> {
  if (Object.keys(p).length) throw new ApiError(400, "PRUNE_PAYLOAD_INVALID");
  const hash = await requestHash("admin.prune", {}); const replay = await priorOperation(db, opId, "admin.prune", hash);
  if (replay) return { status: 200, body: { ...replay, replayed: true } };
  const quotaCutoff = new Date(now.getTime() - 7 * 86400000).toISOString(); const resolvedCutoff = new Date(now.getTime() - 400 * 86400000).toISOString();
  const results = await db.batch([
    operationInsert(db, opId, "admin.prune", hash, now),
    db.prepare("DELETE FROM quota_reservation WHERE created_at < ? AND rowid IN (SELECT rowid FROM quota_reservation WHERE created_at < ? LIMIT 1000) AND changes()=1").bind(quotaCutoff, quotaCutoff),
    db.prepare("DELETE FROM quota_bucket WHERE (period='day' AND bucket_key<?) OR (period='minute' AND bucket_key<?) AND changes()>=0").bind(new Date(now.getTime() - 2 * 86400000).toISOString().slice(0,10), new Date(now.getTime() - 3 * 60000).toISOString().slice(0,16)+"Z"),
    db.prepare("DELETE FROM delivery_recipient WHERE status IN ('SENT','FAILED_SAFE') AND updated_at<? AND rowid IN (SELECT rowid FROM delivery_recipient WHERE status IN ('SENT','FAILED_SAFE') AND updated_at<? LIMIT 1000) AND changes()>=0").bind(resolvedCutoff, resolvedCutoff),
    db.prepare("DELETE FROM run_claim WHERE status IN ('COMPLETE','FAILED') AND finished_at<? AND rowid IN (SELECT rowid FROM run_claim WHERE status IN ('COMPLETE','FAILED') AND finished_at<? LIMIT 1000) AND changes()>=0").bind(resolvedCutoff, resolvedCutoff),
    db.prepare("DELETE FROM operations WHERE created_at<? AND operation_id IN (SELECT operation_id FROM operations WHERE created_at<? LIMIT 1000) AND operation_id<>? AND changes()>=0").bind(quotaCutoff, quotaCutoff, opId),
    operationCommit(db, opId),
  ]);
  if (Number(results[0]?.meta?.changes) !== 1) return replayOrConflict(db, opId, "admin.prune", hash);
  await storeOperationResult(db, opId, { operation_id: opId, applied: true });
  return { status: 200, body: { operation_id: opId, applied: true, replayed: false } };
}

async function requireOwnership(db: D1Database, delivery: boolean, testMode = false): Promise<void> {
  const row = await db.prepare("SELECT cloud_quota_exclusive,windows_sender_disabled,cloud_delivery_enabled FROM ownership WHERE singleton=1").first<JsonObject>();
  if (testMode) {
    if (!row || Number(row.cloud_quota_exclusive) !== 0 || Number(row.windows_sender_disabled) !== 0 || Number(row.cloud_delivery_enabled) !== 0) {
      throw new ApiError(403, "TEST_OWNERSHIP_GATES_MUST_REMAIN_CLOSED");
    }
    return;
  }
  if (!row || Number(row.cloud_quota_exclusive)!==1 || Number(row.windows_sender_disabled)!==1 || (delivery && Number(row.cloud_delivery_enabled)!==1)) throw new ApiError(403, "CLOUD_OWNERSHIP_GATE_CLOSED");
}

function testModeRequested(env: Env): boolean {
  return Boolean(env.MARKETBOT_TEST_MODE || env.MARKETBOT_TEST_DATABASE_ID || env.MARKETBOT_TEST_WORKER_NAME);
}

async function resolveTestMutationMode(env: Env): Promise<boolean> {
  if (!testModeRequested(env)) return false;
  if (env.MARKETBOT_TEST_MODE !== TEST_MODE || env.MARKETBOT_TEST_DATABASE_ID !== TEST_DATABASE_ID || env.MARKETBOT_TEST_WORKER_NAME !== TEST_WORKER_NAME) {
    throw new ApiError(503, "TEST_ENVIRONMENT_CONFIG_INVALID");
  }
  let identity: { database_id: string; worker_name: string; mode: string; attestation_hash: string } | null;
  try {
    identity = await env.DB.prepare("SELECT database_id,worker_name,mode,attestation_hash FROM test_environment_identity WHERE singleton=1").first<{ database_id: string; worker_name: string; mode: string; attestation_hash: string }>();
  } catch {
    throw new ApiError(503, "TEST_ENVIRONMENT_IDENTITY_INVALID");
  }
  const expectedAttestation = await sha256(env.STATE_ADMIN_HMAC_SECRET);
  if (!identity || identity.database_id !== TEST_DATABASE_ID || identity.worker_name !== TEST_WORKER_NAME || identity.mode !== TEST_MODE || !/^[0-9a-f]{64}$/.test(identity.attestation_hash) || !constantTimeEqual(identity.attestation_hash, expectedAttestation)) {
    throw new ApiError(503, "TEST_ENVIRONMENT_IDENTITY_INVALID");
  }
  return true;
}

function validateSyntheticTestRequest(op: string, operationId: string, payload: JsonObject): void {
  if (op === "ownership.set" || op === "admin.prune") throw new ApiError(403, "TEST_ADMIN_OPERATION_FORBIDDEN");
  if (!TEST_ID_RE.test(operationId)) throw new ApiError(400, "TEST_SYNTHETIC_SCOPE_REQUIRED");
  if (op === "quota.reserve") return;
  if (op === "run.claim") {
    if (!isTestDate(payload.market_date) || typeof payload.run_id !== "string" || !TEST_ID_RE.test(payload.run_id)) throw new ApiError(400, "TEST_SYNTHETIC_SCOPE_REQUIRED");
    return;
  }
  if (op === "run.transition" || op === "run.recover") {
    if (!isTestDate(payload.market_date) || typeof payload.run_id !== "string" || !TEST_ID_RE.test(payload.run_id)) throw new ApiError(400, "TEST_SYNTHETIC_SCOPE_REQUIRED");
    return;
  }
  if (op === "delivery.begin" || op === "delivery.outcome" || op === "delivery.reconcile") {
    if (!isTestDate(payload.market_date) || typeof payload.recipient_hmac !== "string" || !TEST_RECIPIENT_SLOTS.has(payload.recipient_hmac)) throw new ApiError(400, "TEST_SYNTHETIC_SCOPE_REQUIRED");
    if (op === "delivery.begin" && (typeof payload.attempt_id !== "string" || !TEST_ID_RE.test(payload.attempt_id))) throw new ApiError(400, "TEST_SYNTHETIC_SCOPE_REQUIRED");
    if (op === "delivery.reconcile" && payload.reviewer_hmac !== TEST_REVIEWER_SLOT) throw new ApiError(400, "TEST_SYNTHETIC_SCOPE_REQUIRED");
    return;
  }
  throw new ApiError(403, "TEST_OPERATION_NOT_ALLOWED");
}

function isTestDate(value: unknown): boolean {
  if (typeof value !== "string" || !TEST_DATE_RE.test(value)) return false;
  const date = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function operationInsert(db: D1Database, id: string, type: string, hash: string, now: Date): D1Statement {
  return db.prepare("INSERT INTO operations(operation_id,operation_type,request_hash,created_at,status) VALUES(?,?,?,?, 'COMMITTED') ON CONFLICT(operation_id) DO NOTHING").bind(id, type, hash, now.toISOString());
}

function validatePayloadKeys(op: string, payload: JsonObject): void {
  const allowed: Record<string, string[]> = {
    "state.get": ["market_date"],
    "quota.reserve": ["provider", "credits"],
    "run.claim": ["market_date", "run_id"],
    "run.transition": ["market_date", "run_id", "from", "to", "reason_code"],
    "run.recover": ["market_date", "run_id"],
    "delivery.begin": ["market_date", "recipient_hmac", "attempt_id", "reviewed_retry"],
    "delivery.outcome": ["market_date", "recipient_hmac", "status", "safe_error"],
    "delivery.reconcile": ["market_date", "recipient_hmac", "reviewer_hmac", "confirmed_sent"],
    "ownership.set": ["cloud_quota_exclusive", "windows_sender_disabled", "cloud_delivery_enabled"],
    "admin.prune": [],
  };
  if (!allowed[op] || Object.keys(payload).some(key => !allowed[op].includes(key))) throw new ApiError(400, "OPERATION_PAYLOAD_INVALID");
}
function operationCommit(db: D1Database, _id: string): D1Statement { return db.prepare("SELECT 1"); }

async function priorOperation(db: D1Database, id: string, type: string, hash: string): Promise<JsonObject | null> {
  const row = await db.prepare("SELECT operation_type,request_hash FROM operations WHERE operation_id=?").bind(id).first<{ operation_type: string; request_hash: string }>();
  if (!row) return null;
  if (row.operation_type !== type || row.request_hash !== hash) throw new ApiError(409, "OPERATION_ID_REUSED_WITH_DIFFERENT_REQUEST");
  // Never return a second external-call authorization after an idempotent replay.
  if (type === "quota.reserve") {
    const reservation = await db.prepare("SELECT reservation_id FROM quota_reservation WHERE reservation_id=?").bind(id).first();
    return reservation ? { operation_id: id, reservation_id: id, applied: true } : { operation_id: id, applied: false };
  }
  if (type === "delivery.begin") {
    const delivery = await db.prepare("SELECT status,attempt,last_operation_id FROM delivery_recipient WHERE last_operation_id=?").bind(id).first<JsonObject>();
    return { operation_id: id, applied: !!delivery, status: delivery?.status, attempt: delivery?.attempt };
  }
  if (type === "run.claim") {
    const claim = await db.prepare("SELECT market_date,run_id FROM run_claim WHERE claim_operation_id=?").bind(id).first<JsonObject>();
    return claim ? { operation_id: id, market_date: claim.market_date, run_id: claim.run_id, claim_authorized: false, applied: true } : { operation_id: id, applied: false };
  }
  if (type === "ownership.set" || type === "admin.prune") return { operation_id: id, applied: true };
  const appliedRow = await db.prepare("SELECT 1 FROM run_claim WHERE last_operation_id=? UNION ALL SELECT 1 FROM delivery_recipient WHERE last_operation_id=? LIMIT 1").bind(id, id).first();
  return { operation_id: id, applied: !!appliedRow };
}
async function replayOrConflict(db: D1Database, id: string, type: string, hash: string): Promise<DispatchResult> {
  const prior = await priorOperation(db, id, type, hash);
  if (prior) return { status: prior.applied === true ? 200 : 409, body: { ...prior, authorized: false, claim_authorized: false, replayed: true } };
  throw new ApiError(409, "STATE_TRANSITION_CONFLICT");
}
async function storeOperationResult(db: D1Database, id: string, _result: JsonObject): Promise<void> {
  // Domain rows carry last_operation_id; replay resolves from that marker. No gap exists
  // between a committed state change and its idempotency marker.
  void db; void id;
}
async function requestHash(op: string, payload: JsonObject): Promise<string> {
  return sha256(JSON.stringify({ op, payload: sortObject(payload) }));
}
function sortObject(input: JsonObject): JsonObject { return Object.fromEntries(Object.keys(input).sort().map(key => [key, input[key]])); }
function requireDate(value: unknown): string { if (!isDate(value)) throw new ApiError(400, "MARKET_DATE_INVALID"); return value; }
function isDate(value: unknown): value is string { if (typeof value !== "string" || !DATE_RE.test(value)) return false; const date = new Date(`${value}T00:00:00.000Z`); return !Number.isNaN(date.getTime()) && date.toISOString().slice(0,10)===value; }
function requireId(value: unknown, code: string): string { if (typeof value !== "string" || !ID_RE.test(value)) throw new ApiError(400, code); return value; }
function requireSlot(value: unknown): string { if (typeof value !== "string" || !SLOT_RE.test(value)) throw new ApiError(400, "RECIPIENT_REFERENCE_INVALID"); return value; }
function requireSafeCode(value: unknown): string { if (typeof value !== "string" || !SAFE_CODE_RE.test(value)) throw new ApiError(400, "SAFE_CODE_INVALID"); return value; }
function requireEnum<T extends string>(value: unknown, allowed: T[]): T { if (typeof value !== "string" || !allowed.includes(value as T)) throw new ApiError(400, "STATE_VALUE_INVALID"); return value as T; }
function validRunTransition(from: string, to: string): boolean { const allowed: Record<string,string[]> = { CLAIMED:["RUNNING","FAILED"], RUNNING:["REPORT_READY","DELIVERY_IN_PROGRESS","COMPLETE","FAILED","PARTIAL_DELIVERY","DELIVERY_UNKNOWN"], REPORT_READY:["DELIVERY_IN_PROGRESS","COMPLETE","FAILED"], DELIVERY_IN_PROGRESS:["COMPLETE","PARTIAL_DELIVERY","DELIVERY_UNKNOWN","FAILED"] }; return allowed[from]?.includes(to)===true; }
async function sha256(value: string): Promise<string> { const bytes = new TextEncoder().encode(value); const digest = await crypto.subtle.digest("SHA-256", bytes); return [...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,"0")).join(""); }
async function hmacHex(secret: string, value: string): Promise<string> { const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), {name:"HMAC",hash:"SHA-256"}, false, ["sign"]); const signature=await crypto.subtle.sign("HMAC",key,new TextEncoder().encode(value)); return [...new Uint8Array(signature)].map(b=>b.toString(16).padStart(2,"0")).join(""); }
function constantTimeEqual(a: string,b:string): boolean { if (a.length!==b.length) return false; let diff=0; for(let i=0;i<a.length;i++) diff |= a.charCodeAt(i)^b.charCodeAt(i); return diff===0; }
function json(value: JsonObject, status=200): Response { return new Response(JSON.stringify(value), {status,headers:{"content-type":"application/json; charset=utf-8","cache-control":"no-store"}}); }

export default { fetch: (request: Request, env: Env) => handleRequest(request, env) };
