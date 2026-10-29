import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import crypto from "node:crypto";

const projectDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const wrangler = path.join(projectDir, "node_modules", "wrangler", "bin", "wrangler.js");
const dbName = "marketbot-local-test";
const apiSecret = "synthetic-test-secret-".padEnd(48, "x");
const adminSecret = "synthetic-test-admin-secret-".padEnd(48, "y");
const baseUrl = "http://127.0.0.1:8787";
const tempRoot = await mkdtemp(path.join(os.tmpdir(), "marketbot-d1-local-"));
const localConfig = path.join(tempRoot, "wrangler.local.json");
const passed = [];
let server;
let serverOutput = "";
let activeTest = "startup";

function cli(args) {
  if (!args.includes("--local") || args.includes("--remote")) throw new Error("LOCAL_ONLY_COMMAND_REQUIRED");
  const result = spawnSync(process.execPath, [wrangler, ...args, "--config", localConfig], {
    cwd: projectDir,
    encoding: "utf8",
    timeout: 45_000,
    env: { ...process.env, WRANGLER_SEND_METRICS: "false", CLOUDFLARE_SEND_METRICS: "false" },
  });
  if (result.status !== 0) {
    const diagnostic = `${result.stderr || ""}\n${result.stdout || ""}`.replace(/\s+/g, " ").slice(-1000);
    throw new Error(`WRANGLER_LOCAL_COMMAND_FAILED:${args.slice(0, 3).join("_")}:${result.status}:${diagnostic}`);
  }
  return result.stdout;
}

function sqlRows(query) {
  const output = cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--json", "--command", query]);
  const parsed = JSON.parse(output);
  return parsed[0]?.results ?? [];
}

async function withDatabaseCli(callback) {
  await stopServer();
  try { return callback(); }
  finally { await startServer(); }
}

async function startServer({ testMode = false, testDatabaseId = "8049a22c-eb1f-4e9d-928d-29b35dcc6f70", testAdminSecret = adminSecret } = {}) {
  if (server && server.exitCode === null) return;
  serverOutput = "";
  const args = [wrangler, "dev", "--config", localConfig, "--local", "--ip", "127.0.0.1", "--port", "8787", "--persist-to", tempRoot, "--var", `STATE_API_HMAC_SECRET:${apiSecret}`, "--var", `STATE_ADMIN_HMAC_SECRET:${testAdminSecret}`];
  if (testMode) args.push("--var", "MARKETBOT_TEST_MODE:phase3c-synthetic-mutations-v1", "--var", `MARKETBOT_TEST_DATABASE_ID:${testDatabaseId}`, "--var", "MARKETBOT_TEST_WORKER_NAME:marketbot-state-api-test");
  server = spawn(process.execPath, args, {
    cwd: projectDir,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, WRANGLER_SEND_METRICS: "false", CLOUDFLARE_SEND_METRICS: "false" },
  });
  server.stdout.on("data", chunk => { serverOutput = (serverOutput + chunk.toString()).slice(-4000); });
  server.stderr.on("data", chunk => { serverOutput = (serverOutput + chunk.toString()).slice(-4000); });
  await waitReady();
}

async function stopServer() {
  if (!server || server.exitCode !== null) return;
  const child = server;
  if (process.platform === "win32" && child.pid) {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { encoding: "utf8", timeout: 5000 });
  } else {
    child.kill("SIGTERM");
  }
  await Promise.race([
    new Promise(resolve => child.once("close", resolve)),
    new Promise(resolve => setTimeout(resolve, 3000)),
  ]);
  server = undefined;
}

async function waitReady() {
  const deadline = Date.now() + 45_000;
  while (Date.now() < deadline) {
    if (server.exitCode !== null) throw new Error("WRANGLER_LOCAL_SERVER_EXITED");
    try {
      const response = await fetch(`${baseUrl}/`);
      if (response.status === 404) return;
    } catch { /* Local server is still starting. */ }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error("WRANGLER_LOCAL_SERVER_START_TIMEOUT");
}

async function request(op, payload, { admin = false, operationId = crypto.randomBytes(16).toString("hex"), secret } = {}) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = crypto.randomBytes(16).toString("hex");
  const body = JSON.stringify({ schema_version: 1, operation_id: operationId, op, payload });
  const signingKey = secret ?? (admin ? adminSecret : apiSecret);
  const signature = crypto.createHmac("sha256", signingKey).update(`${timestamp}.${nonce}.${body}`).digest("hex");
  const response = await fetch(`${baseUrl}/v1/state`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-mb-timestamp": timestamp,
      "x-mb-nonce": nonce,
      [admin ? "x-mb-admin-signature" : "x-mb-signature"]: signature,
    },
    body,
  });
  return { status: response.status, data: await response.json() };
}

function check(name, callback) {
  activeTest = name;
  return Promise.resolve().then(callback).then(() => passed.push(name));
}

try {
  await writeFile(localConfig, JSON.stringify({
    name: "marketbot-local-test", main: path.join(projectDir, "src", "index.ts"),
    compatibility_date: "2026-10-14",
    d1_databases: [{ binding: "DB", database_name: dbName,
      database_id: "00000000-0000-4000-8000-000000000001",
      migrations_dir: path.join(projectDir, "migrations") }],
  }));
  cli(["d1", "migrations", "apply", dbName, "--local", "--persist-to", tempRoot]);
  await startServer();

  await check("auth rejection and malformed schema", async () => {
    const publicRoot = await fetch(`${baseUrl}/`);
    const publicHealth = await fetch(`${baseUrl}/health`);
    const unauthenticatedGet = await fetch(`${baseUrl}/v1/state`);
    assert.equal(publicRoot.status, 404);
    assert.equal(publicHealth.status, 404);
    assert.equal(unauthenticatedGet.status, 404);
    const noAuth = await fetch(`${baseUrl}/v1/state`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
    assert.equal(noAuth.status, 400);
    const validBodyWithoutAuth = JSON.stringify({ schema_version: 1, operation_id: crypto.randomBytes(16).toString("hex"), op: "state.get", payload: {} });
    const validNoAuth = await fetch(`${baseUrl}/v1/state`, { method: "POST", headers: { "content-type": "application/json" }, body: validBodyWithoutAuth });
    assert.equal(validNoAuth.status, 401);
    const badAuth = await request("state.get", {}, { secret: "incorrect-synthetic-secret" });
    assert.equal(badAuth.status, 401, JSON.stringify(badAuth.data));
    const adminWithoutAdminKey = await request("ownership.set", { cloud_quota_exclusive: true, windows_sender_disabled: true, cloud_delivery_enabled: true });
    assert.equal(adminWithoutAdminKey.status, 401);
    await withDatabaseCli(() => {
      assert.equal(sqlRows("SELECT COUNT(*) AS n FROM operations")[0].n, 0);
      assert.equal(sqlRows("SELECT COUNT(*) AS n FROM request_nonces")[0].n, 0);
      assert.deepEqual(sqlRows("SELECT cloud_quota_exclusive,windows_sender_disabled,cloud_delivery_enabled FROM ownership WHERE singleton=1")[0], {
        cloud_quota_exclusive: 0, windows_sender_disabled: 0, cloud_delivery_enabled: 0,
      });
    });
    const malformed = await request("state.get", {}, { operationId: crypto.randomBytes(16).toString("hex") });
    assert.equal(malformed.status, 200);
    assert.deepEqual(malformed.data.quota, { buckets: [], reservations_by_utc_day: [] });
    assert.deepEqual(malformed.data.deliveries_by_market_date_and_status, []);
    assert.deepEqual(malformed.data.operations_by_type_and_status, []);
    assert.equal(malformed.data.integrity_window_days, 7);
    assert.deepEqual(malformed.data.integrity, {
      reservations_without_operation: 0,
      recent_runs_without_claim_operation: 0,
      recent_runs_without_last_operation: 0,
      recent_deliveries_without_last_operation: 0,
      deliveries_without_run: 0,
      completed_runs_with_unsent_delivery: 0,
      daily_quota_bucket_mismatches: 0,
      minute_quota_bucket_mismatches: 0,
    });
    const badVersionBody = JSON.stringify({ schema_version: 99, operation_id: crypto.randomBytes(16).toString("hex"), op: "state.get", payload: {} });
    const badVersionResponse = await fetch(`${baseUrl}/v1/state`, { method: "POST", headers: { "content-type": "application/json" }, body: badVersionBody });
    assert.equal(badVersionResponse.status, 400);
  });

  await check("ownership admin gate defaults closed", async () => {
    const state = await request("state.get", {});
    assert.deepEqual(state.data.ownership, { cloud_quota_exclusive: false, windows_sender_disabled: false, cloud_delivery_enabled: false });
    assert.equal((await request("quota.reserve", { provider: "twelve_data", credits: 1 })).status, 403);
  });

  await check("expanded authenticated state.get is business-read-only and omits recipient identifiers", async () => {
    const counts = () => sqlRows("SELECT (SELECT COUNT(*) FROM operations) AS operations,(SELECT COUNT(*) FROM quota_reservation) AS reservations,(SELECT COUNT(*) FROM quota_bucket) AS buckets,(SELECT COUNT(*) FROM run_claim) AS runs,(SELECT COUNT(*) FROM delivery_recipient) AS deliveries,(SELECT cloud_quota_exclusive*100+windows_sender_disabled*10+cloud_delivery_enabled FROM ownership WHERE singleton=1) AS gates")[0];
    const before = await withDatabaseCli(() => counts());
    const response = await request("state.get", {});
    assert.equal(response.status, 200);
    assert.deepEqual(response.data.ownership, { cloud_quota_exclusive: false, windows_sender_disabled: false, cloud_delivery_enabled: false });
    assert.equal(JSON.stringify(response.data).includes("hmac256:"), false);
    const after = await withDatabaseCli(() => counts());
    assert.deepEqual(after, before, "state.get must not alter operational D1 tables or ownership");
  });

  await check("isolated synthetic mode requires environment plus D1 identity and preserves closed gates", async () => {
    await stopServer();
    const template = await readFile(path.join(projectDir, "scripts", "phase3c_test_identity.sql"), "utf8");
    const adminHash = crypto.createHash("sha256").update(adminSecret).digest("hex");
    const identitySql = path.join(tempRoot, "phase3c-test-identity.sql");
    await writeFile(identitySql, template.replace("__TEST_ADMIN_SECRET_SHA256__", adminHash), "utf8");
    cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--file", identitySql]);
    await startServer({ testMode: true });
    const state = await request("state.get", {});
    assert.equal(state.status, 200);
    assert.equal(state.data.test_mutation_mode, true);
    const protocol2 = spawnSync("python", [path.join(projectDir, "scripts", "protocol2_integration.py"), "--local", "--journal-dir", tempRoot], {
      cwd: projectDir, encoding: "utf8", timeout: 60_000,
    });
    assert.equal(protocol2.status, 0, protocol2.stdout + protocol2.stderr);
    assert.match(protocol2.stdout, /PROTOCOL2_INTEGRATION=PASS/);
    console.log(protocol2.stdout.trim());

    assert.deepEqual(state.data.ownership, { cloud_quota_exclusive: false, windows_sender_disabled: false, cloud_delivery_enabled: false });

    const prefixId = (suffix) => `3c${suffix.padStart(30, "0")}`;
    const quota = await request("quota.reserve", { provider: "twelve_data", credits: 1 }, { operationId: prefixId("1") });
    assert.equal(quota.status, 200);
    assert.equal(quota.data.authorized, true);

    const date = "2099-01-01";
    const runId = prefixId("2");
    const claim = await request("run.claim", { market_date: date, run_id: runId }, { operationId: prefixId("3") });
    assert.equal(claim.status, 200);
    assert.equal(claim.data.claim_authorized, true);
    assert.equal((await request("run.transition", { market_date: date, run_id: runId, from: "CLAIMED", to: "RUNNING" }, { operationId: prefixId("d") })).status, 200);
    const slot = "hmac256:ac00581af76fae2942fefc401985c751c927c4412b3e8486f54cda8e4d420419";
    const reviewer = "hmac256:ad4e4df2c02723b74f014769278cd2c1edac43e7f17751f1b9048f40b11aa692";
    const begin = await request("delivery.begin", { market_date: date, recipient_hmac: slot, attempt_id: prefixId("4"), reviewed_retry: false }, { operationId: prefixId("5") });
    assert.equal(begin.status, 200);
    assert.equal(begin.data.authorized, true);
    const unknown = await request("delivery.outcome", { market_date: date, recipient_hmac: slot, attempt_id: prefixId("4"), status: "UNKNOWN", safe_error: "SIMULATED_RESPONSE_LOSS" }, { operationId: prefixId("6") });
    assert.equal(unknown.status, 200);
    const noAutomaticRetry = await request("delivery.begin", { market_date: date, recipient_hmac: slot, attempt_id: prefixId("7"), reviewed_retry: true, sender_stopped: true }, { operationId: prefixId("8"), admin: true });
    assert.equal(noAutomaticRetry.status, 409);
    const reconciled = await request("delivery.reconcile", { market_date: date, recipient_hmac: slot, reviewer_hmac: reviewer, confirmed_sent: false }, { operationId: prefixId("9"), admin: true });
    assert.equal(reconciled.status, 200);
    assert.equal(reconciled.data.applied, true);
    const reviewedRetry = await request("delivery.begin", { market_date: date, recipient_hmac: slot, attempt_id: prefixId("a"), reviewed_retry: true, sender_stopped: true }, { operationId: prefixId("b"), admin: true });
    assert.equal(reviewedRetry.status, 200);
    assert.equal(reviewedRetry.data.authorized, true);

    const realLooking = await request("run.claim", { market_date: "2026-10-14", run_id: crypto.randomBytes(16).toString("hex") });
    assert.equal(realLooking.status, 400);
    assert.equal(realLooking.data.error, "TEST_SYNTHETIC_SCOPE_REQUIRED");
    const ownershipAttempt = await request("ownership.set", { cloud_quota_exclusive: true, windows_sender_disabled: true, cloud_delivery_enabled: true }, { admin: true, operationId: prefixId("c") });
    assert.equal(ownershipAttempt.status, 403);
    assert.equal(ownershipAttempt.data.error, "TEST_ADMIN_OPERATION_FORBIDDEN");
    const after = await request("state.get", {});
    assert.deepEqual(after.data.ownership, { cloud_quota_exclusive: false, windows_sender_disabled: false, cloud_delivery_enabled: false });
    assert.equal(after.data.quota.reservations_by_utc_day.some((row) => row.credits >= 1), true);
    assert.equal(after.data.deliveries_by_market_date_and_status.some((row) => row.market_date === date && row.status === "SENDING" && row.recipient_count === 1), true);
    assert.equal(after.data.operations_by_type_and_status.some((row) => row.operation_type === "delivery.begin" && row.status === "COMMITTED"), true);
    assert.equal(Object.values(after.data.integrity).every((count) => count === 0), true);
    assert.equal(JSON.stringify(after.data).includes(slot), false, "state.get must not disclose recipient HMACs");

    await stopServer();
    await startServer({ testMode: true, testAdminSecret: "wrong-local-admin-secret".padEnd(48, "z") });
    const wrongSecretMarker = await request("state.get", {});
    assert.equal(wrongSecretMarker.status, 503);
    assert.equal(wrongSecretMarker.data.error, "TEST_ENVIRONMENT_IDENTITY_INVALID");
    await stopServer();
    cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", "DROP TABLE test_environment_identity"]);
    await startServer({ testMode: true });
    const missingIdentity = await request("state.get", {});
    assert.equal(missingIdentity.status, 503);
    assert.equal(missingIdentity.data.error, "TEST_ENVIRONMENT_IDENTITY_INVALID");
    await stopServer();
    await startServer({ testMode: true, testDatabaseId: "3f40e99b-8fb3-4fb8-90c7-ea1b4e019061" });
    const productionIdRejected = await request("state.get", {});
    assert.equal(productionIdRejected.status, 503);
    assert.equal(productionIdRejected.data.error, "TEST_ENVIRONMENT_CONFIG_INVALID");
    await stopServer();
    cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--file", identitySql]);
    const syntheticOp = `3c${"d".repeat(30)}`;
    const syntheticRun = `3c${"e".repeat(30)}`;
    const syntheticLastOp = `3c${"f".repeat(30)}`;
    const syntheticDate = "2099-01-04";
    const utcDay = "2099-01-04";
    const utcMinute = "2099-01-04T00:00Z";
    const seededAt = new Date().toISOString();
    const rowsSql = path.join(tempRoot, "phase3c-test-cleanup-fixture.sql");
    await writeFile(rowsSql, `INSERT INTO operations(operation_id,operation_type,request_hash,created_at,status) VALUES ('${syntheticOp}','quota.reserve','${"a".repeat(64)}','${seededAt}','COMMITTED');\nINSERT INTO quota_bucket(period,bucket_key,used_credits,limit_credits) VALUES ('day','${utcDay}',1,800),('minute','${utcMinute}',1,8);\nINSERT INTO quota_reservation(reservation_id,provider,utc_day,utc_minute,credits,created_at) VALUES ('${syntheticOp}','twelve_data','${utcDay}','${utcMinute}',1,'${seededAt}');\nINSERT INTO run_claim(market_date,run_id,status,claim_operation_id,last_operation_id,claimed_at) VALUES ('${syntheticDate}','${syntheticRun}','COMPLETE','${syntheticOp}','${syntheticLastOp}','${seededAt}');\nINSERT INTO delivery_recipient(market_date,recipient_hmac,status,attempt,attempt_id,last_operation_id,send_started_at,updated_at) VALUES ('${syntheticDate}','hmac256:ac00581af76fae2942fefc401985c751c927c4412b3e8486f54cda8e4d420419','SENT',1,'${syntheticOp}','${syntheticLastOp}','${seededAt}','${seededAt}');\nINSERT INTO audit_event(audit_id,event_type,subject_ref,actor_ref,created_at) VALUES ('${syntheticLastOp}','SYNTHETIC','2099-01-04:TEST','TEST','${seededAt}');\n`, "utf8");
    cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--file", rowsSql]);
    const cleanupTemplate = await readFile(path.join(projectDir, "scripts", "cleanup_phase3c_test_data.sql"), "utf8");
    const cleanupSql = path.join(tempRoot, "phase3c-test-cleanup.sql");
    await writeFile(cleanupSql, cleanupTemplate.replaceAll("__TEST_ADMIN_SECRET_SHA256__", adminHash), "utf8");
    cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--file", cleanupSql]);
    assert.equal(sqlRows("SELECT COUNT(*) AS n FROM operations")[0].n, 0);
    assert.equal(sqlRows("SELECT COUNT(*) AS n FROM quota_reservation")[0].n, 0);
    assert.equal(sqlRows("SELECT COUNT(*) AS n FROM quota_bucket")[0].n, 0);
    assert.equal(sqlRows("SELECT COUNT(*) AS n FROM run_claim")[0].n, 0);
    assert.equal(sqlRows("SELECT COUNT(*) AS n FROM delivery_recipient")[0].n, 0);
    assert.equal(sqlRows("SELECT COUNT(*) AS n FROM audit_event")[0].n, 0);
    assert.equal(sqlRows("SELECT COUNT(*) AS n FROM test_environment_identity")[0].n, 0);
    assert.deepEqual(sqlRows("SELECT cloud_quota_exclusive,windows_sender_disabled,cloud_delivery_enabled FROM ownership WHERE singleton=1")[0], { cloud_quota_exclusive: 0, windows_sender_disabled: 0, cloud_delivery_enabled: 0 });
    await startServer();
  });

  await check("admin operation opens only explicit local test gates", async () => {
    const response = await request("ownership.set", { cloud_quota_exclusive: true, windows_sender_disabled: true, cloud_delivery_enabled: true }, { admin: true });
    assert.equal(response.status, 200);
    assert.equal((await request("state.get", {})).data.ownership.cloud_delivery_enabled, true);
  });

  await check("protocol 2 recovery rollback in real local D1", async () => {
    const date = "2099-03-01", runId = crypto.randomBytes(16).toString("hex");
    const slot = "hmac256:" + "f".repeat(64);
    assert.equal((await request("run.claim", {market_date: date, run_id: runId})).status, 200);
    assert.equal((await request("run.transition", {market_date: date, run_id: runId, from: "CLAIMED", to: "RUNNING"})).status, 200);
    const begin = await request("delivery.begin", {market_date: date, recipient_hmac: slot, attempt_id: crypto.randomBytes(16).toString("hex"), reviewed_retry: false});
    assert.equal(begin.data.authorized, true);
    await withDatabaseCli(() => sqlRows("CREATE TRIGGER protocol2_failure BEFORE UPDATE ON delivery_recipient WHEN OLD.market_date='2099-03-01' BEGIN SELECT RAISE(ABORT,'synthetic'); END"));
    const operationId = crypto.randomBytes(16).toString("hex");
    const payload = {market_date: date, run_id: runId, sender_stopped: true};
    assert.equal((await request("run.recover", payload, {admin: true, operationId})).status, 503);
    const state = await request("state.get", {market_date: date, recipient_hmac: slot});
    assert.equal(state.data.run.status, "RUNNING");
    assert.equal(state.data.delivery_recipient.status, "SENDING");
    assert.equal(state.data.delivery_recipient.attempt_id, begin.data.attempt_id);
    await withDatabaseCli(() => {
      assert.equal(sqlRows(`SELECT COUNT(*) AS n FROM operations WHERE operation_id='${operationId}'`)[0].n, 0);
      sqlRows("DROP TRIGGER protocol2_failure");
    });
    assert.equal((await request("run.recover", payload, {admin: true})).data.status, "DELIVERY_UNKNOWN");
  });

  await check("concurrent quota contention allows exactly eight of sixteen", async () => {
    const results = await Promise.all(Array.from({ length: 16 }, () => request("quota.reserve", { provider: "twelve_data", credits: 1 })));
    assert.equal(results.filter(item => item.status === 200 && item.data.authorized === true).length, 8);
    assert.equal(results.filter(item => item.status === 429).length, 8);
    await withDatabaseCli(() => {
      assert.equal(sqlRows("SELECT used_credits FROM quota_bucket WHERE period='minute'")[0].used_credits, 8);
      assert.equal(sqlRows("SELECT COUNT(*) AS n FROM quota_reservation")[0].n, 8);
    });
  });

  await check("same-ID retry after simulated lost response never grants second provider call", async () => {
    await withDatabaseCli(() => cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", "DELETE FROM quota_reservation; DELETE FROM quota_bucket;"]));
    const opId = crypto.randomBytes(16).toString("hex");
    const first = await request("quota.reserve", { provider: "twelve_data", credits: 2 }, { operationId: opId });
    assert.equal(first.data.authorized, true, `quota first response status=${first.status} code=${first.data.error || "none"}`); // Treat the HTTP response as lost after commit.
    const retry = await request("quota.reserve", { provider: "twelve_data", credits: 2 }, { operationId: opId });
    assert.equal(retry.data.authorized, false);
    assert.equal(retry.data.replayed, true);
    assert.equal(sqlRows("SELECT used_credits FROM quota_bucket WHERE period='minute'")[0].used_credits, 2);
    assert.equal((await request("quota.reserve", { provider: "twelve_data", credits: 1 }, { operationId: opId })).status, 409);
  });

  await check("D1 batch rollback leaves no quota charge after injected database error", async () => {
    const countsBefore = await withDatabaseCli(() => ({
      operations: sqlRows("SELECT COUNT(*) AS n FROM operations")[0].n,
      reservations: sqlRows("SELECT COUNT(*) AS n FROM quota_reservation")[0].n,
      dailyCredits: sqlRows("SELECT used_credits FROM quota_bucket WHERE period='day'")[0].used_credits,
    }));
    await withDatabaseCli(() => cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", "CREATE TRIGGER test_fail_reservation BEFORE INSERT ON quota_reservation BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END"]));
    const response = await request("quota.reserve", { provider: "twelve_data", credits: 1 });
    assert.equal(response.status, 503);
    await withDatabaseCli(() => {
      assert.equal(sqlRows("SELECT COUNT(*) AS n FROM operations")[0].n, countsBefore.operations);
      assert.equal(sqlRows("SELECT COUNT(*) AS n FROM quota_reservation")[0].n, countsBefore.reservations);
      assert.equal(sqlRows("SELECT used_credits FROM quota_bucket WHERE period='day'")[0].used_credits, countsBefore.dailyCredits);
      cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", "DROP TRIGGER test_fail_reservation"]);
    });
  });

  await check("old UTC day/minute buckets do not consume the current reservation window", async () => {
    const priorDay = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    const priorMinute = new Date(Date.now() - 10 * 60000).toISOString().slice(0, 16) + "Z";
    await withDatabaseCli(() => {
      cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", `INSERT INTO quota_bucket(period,bucket_key,used_credits,limit_credits) VALUES ('day','${priorDay}',800,800),('minute','${priorMinute}',8,8)`]);
    });
    const result = await request("quota.reserve", { provider: "twelve_data", credits: 1 });
    assert.equal(result.status, 200);
    assert.equal(result.data.authorized, true);
  });

  await check("same-date concurrent run claims admit one owner", async () => {
    const results = await Promise.all(Array.from({ length: 8 }, () => request("run.claim", { market_date: "2026-10-14", run_id: crypto.randomBytes(16).toString("hex") })));
    assert.equal(results.filter(item => item.status === 200 && item.data.claim_authorized === true).length, 1);
    assert.equal(results.filter(item => item.status === 409).length, 7);
  });

  await check("delivery claim is one-use; ambiguous outcome remains UNKNOWN until admin reconciliation", async () => {
    const slot = `hmac256:${"a".repeat(64)}`;
    const run = (await request("state.get", { market_date: "2026-10-14" })).data.run;
    assert.equal((await request("run.transition", { market_date: "2026-10-14", run_id: run.run_id, from: "CLAIMED", to: "RUNNING" })).status, 200);
    const first = await request("delivery.begin", { market_date: "2026-10-14", recipient_hmac: slot, attempt_id: crypto.randomBytes(16).toString("hex"), reviewed_retry: false });
    assert.equal(first.data.authorized, true);
    const replay = await request("delivery.begin", { market_date: "2026-10-14", recipient_hmac: slot, attempt_id: crypto.randomBytes(16).toString("hex"), reviewed_retry: false });
    assert.equal(replay.status, 409);
    assert.equal((await request("delivery.outcome", { market_date: "2026-10-14", recipient_hmac: slot, attempt_id: first.data.attempt_id, status: "UNKNOWN", safe_error: "TELEGRAM_TIMEOUT" })).status, 200);
    assert.equal((await request("delivery.begin", { market_date: "2026-10-14", recipient_hmac: slot, attempt_id: crypto.randomBytes(16).toString("hex"), reviewed_retry: true, sender_stopped: true }, { admin: true })).status, 409);
    const reviewed = await request("delivery.reconcile", { market_date: "2026-10-14", recipient_hmac: slot, reviewer_hmac: `hmac256:${"b".repeat(64)}`, confirmed_sent: false }, { admin: true });
    assert.equal(reviewed.data.applied, true);
    const retry = await request("delivery.begin", { market_date: "2026-10-14", recipient_hmac: slot, attempt_id: crypto.randomBytes(16).toString("hex"), reviewed_retry: true, sender_stopped: true }, { admin: true });
    assert.equal(retry.data.authorized, true);
    const mixed = await request("delivery.begin", { market_date: "2026-10-14", recipient_hmac: `hmac256:${"c".repeat(64)}`, attempt_id: crypto.randomBytes(16).toString("hex"), reviewed_retry: false });
    assert.equal(mixed.data.authorized, true);
    await request("delivery.outcome", { market_date: "2026-10-14", recipient_hmac: `hmac256:${"c".repeat(64)}`, attempt_id: mixed.data.attempt_id, status: "SENT" });
  });

  await check("schema mismatch fails closed", async () => {
    await withDatabaseCli(() => cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", "UPDATE schema_meta SET version=88 WHERE singleton=1"]));
    const response = await request("state.get", {});
    assert.equal(response.status, 503);
    assert.equal(response.data.error, "SCHEMA_VERSION_MISMATCH");
    await withDatabaseCli(() => cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", "UPDATE schema_meta SET version=1 WHERE singleton=1"]));
  });

  await check("retention removes old resolved state but retains UNKNOWN delivery", async () => {
    const oldRunId = crypto.randomBytes(16).toString("hex");
    await withDatabaseCli(() => {
      cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", `INSERT INTO run_claim(market_date,run_id,status,claim_operation_id,last_operation_id,claimed_at,finished_at) VALUES ('2025-01-01','${oldRunId}','DELIVERY_UNKNOWN','${crypto.randomBytes(16).toString("hex")}','${crypto.randomBytes(16).toString("hex")}','2025-01-01T00:00:00.000Z','2025-01-01T00:00:00.000Z')`]);
      cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", `INSERT INTO delivery_recipient(market_date,recipient_hmac,status,attempt,attempt_id,last_operation_id,send_started_at,updated_at) VALUES ('2025-01-01','hmac256:${"d".repeat(64)}','UNKNOWN',1,'${crypto.randomBytes(16).toString("hex")}','${crypto.randomBytes(16).toString("hex")}','2025-01-01T00:00:00.000Z','2025-01-01T00:00:00.000Z')`]);
      cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", `INSERT INTO run_claim(market_date,run_id,status,claim_operation_id,last_operation_id,claimed_at,finished_at) VALUES ('2025-01-02','${crypto.randomBytes(16).toString("hex")}','COMPLETE','${crypto.randomBytes(16).toString("hex")}','${crypto.randomBytes(16).toString("hex")}','2025-01-02T00:00:00.000Z','2025-01-02T00:00:00.000Z')`]);
      cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", `INSERT INTO delivery_recipient(market_date,recipient_hmac,status,attempt,attempt_id,last_operation_id,send_started_at,updated_at) VALUES ('2025-01-02','hmac256:${"e".repeat(64)}','SENT',1,'${crypto.randomBytes(16).toString("hex")}','${crypto.randomBytes(16).toString("hex")}','2025-01-02T00:00:00.000Z','2025-01-02T00:00:00.000Z')`]);
      cli(["d1", "execute", dbName, "--local", "--persist-to", tempRoot, "--command", `INSERT INTO quota_reservation(reservation_id,provider,utc_day,utc_minute,credits,created_at) VALUES ('${crypto.randomBytes(16).toString("hex")}','twelve_data','2025-01-01','2025-01-01T00:00Z',1,'2025-01-01T00:00:00.000Z')`]);
    });
    const result = await request("admin.prune", {}, { admin: true });
    assert.equal(result.status, 200);
    await withDatabaseCli(() => {
      assert.equal(sqlRows("SELECT status FROM delivery_recipient WHERE market_date='2025-01-01'")[0].status, "UNKNOWN");
      assert.equal(sqlRows("SELECT COUNT(*) AS n FROM delivery_recipient WHERE market_date='2025-01-02'")[0].n, 0);
      assert.equal(sqlRows("SELECT COUNT(*) AS n FROM run_claim WHERE market_date='2025-01-02'")[0].n, 0);
      assert.equal(sqlRows("SELECT COUNT(*) AS n FROM quota_reservation WHERE created_at<'2025-01-01T00:00:00.000Z'")[0].n, 0);
    });
  });

  await check("responses and logs do not disclose secrets or recipient identifiers", async () => {
    const output = await request("state.get", {});
    const text = JSON.stringify(output.data) + serverOutput;
    assert.equal(text.includes(apiSecret), false);
    assert.equal(text.includes(adminSecret), false);
    assert.equal(text.includes("123456789"), false);
  });

  console.log(`WORKER_LOCAL_D1_TESTS: PASS=${passed.length} FAIL=0`);
  for (const name of passed) console.log(`PASS ${name}`);
} catch (error) {
  console.error(`WORKER_LOCAL_D1_TESTS: FAIL at ${activeTest} after ${passed.length} passes (${error?.message || "UNKNOWN"})`);
  if (serverOutput) console.error(serverOutput.slice(-1200));
  process.exitCode = 1;
} finally {
  await stopServer();
  const resolvedTemp = path.resolve(tempRoot);
  if (!resolvedTemp.startsWith(path.resolve(os.tmpdir()) + path.sep)) throw new Error("TEST_TEMP_PATH_OUTSIDE_SYSTEM_TEMP");
  let removed = false;
  for (let retry = 0; retry < 12 && !removed; retry += 1) {
    try { await rm(resolvedTemp, { recursive: true, force: true }); removed = true; }
    catch (error) {
      if (error?.code !== "EBUSY" && error?.code !== "EPERM") throw error;
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  }
  if (!removed) console.warn("LOCAL_D1_TEMP_CLEANUP_PENDING (synthetic isolated test data only)");
}
