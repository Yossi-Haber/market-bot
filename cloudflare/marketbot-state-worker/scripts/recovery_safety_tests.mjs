// Real Worker handlers and SQL, isolated in memory; no network or credentials.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";

const root = new URL("../", import.meta.url);
const require = createRequire(new URL("package.json", root));
const source = readFileSync(new URL("src/index.ts", root), "utf8");
const compiled = require("esbuild").transformSync(source, { loader: "ts", format: "esm", target: "es2022" }).code;
const { handleRequest } = await import("data:text/javascript;base64," + Buffer.from(compiled).toString("base64"));
const schema = readFileSync(new URL("migrations/0001_initial.sql", root), "utf8");
const secret = "synthetic-offline-api-secret-".padEnd(48, "x");
const adminSecret = "synthetic-offline-admin-secret-".padEnd(48, "y");
const clock = () => new Date("2026-10-29T22:00:00.000Z");
const id = () => crypto.randomBytes(16).toString("hex");
const slot = "hmac256:" + "a".repeat(64);
const otherSlot = "hmac256:" + "b".repeat(64);
let passed = 0;

function fixture() {
  const db = new DatabaseSync(":memory:");
  db.exec(schema);
  db.exec("UPDATE ownership SET cloud_quota_exclusive=1,windows_sender_disabled=1,cloud_delivery_enabled=1");
  const adapter = {
    beforeBatch: null,
    prepare(sql) {
      const bound = (params = []) => ({
        sql, params,
        bind(...values) { return bound(values); },
        async first() { return db.prepare(sql).get(...params) ?? null; },
        async all() { return { results: db.prepare(sql).all(...params) }; },
        run() { return { success: true, meta: { changes: Number(db.prepare(sql).run(...params).changes) } }; },
      });
      return bound();
    },
    async batch(statements) {
      // No await within the transaction: each D1 batch is atomic. Concurrent
      // handler preconditions may interleave before they reach this boundary.
      adapter.beforeBatch?.(statements);
      db.exec("BEGIN");
      try {
        const results = statements.map(statement => statement.run());
        db.exec("COMMIT");
        return results;
      } catch (error) { db.exec("ROLLBACK"); throw error; }
    },
  };
  async function call(op, payload, { admin = false, operationId = id() } = {}) {
    const body = JSON.stringify({ schema_version: 1, operation_id: operationId, op, payload });
    const timestamp = String(Math.floor(clock().getTime() / 1000));
    const nonce = id();
    const signature = crypto.createHmac("sha256", admin ? adminSecret : secret).update(`${timestamp}.${nonce}.${body}`).digest("hex");
    const response = await handleRequest(new Request("https://offline.invalid/v1/state", {
      method: "POST", body, headers: {
        "content-type": "application/json", "x-mb-timestamp": timestamp,
        "x-mb-nonce": nonce, [admin ? "x-mb-admin-signature" : "x-mb-signature"]: signature,
      },
    }), { DB: adapter, STATE_API_HMAC_SECRET: secret, STATE_ADMIN_HMAC_SECRET: adminSecret }, clock);
    return { status: response.status, data: await response.json(), operationId };
  }
  const run = day => db.prepare("SELECT * FROM run_claim WHERE market_date=?").get(day);
  const recipient = (day, target = slot) => db.prepare("SELECT * FROM delivery_recipient WHERE market_date=? AND recipient_hmac=?").get(day, target);
  async function claim(day = "2026-10-27", running = true) {
    const runId = id();
    assert.equal((await call("run.claim", { market_date: day, run_id: runId })).status, 200);
    if (running) assert.equal((await call("run.transition", { market_date: day, run_id: runId, from: "CLAIMED", to: "RUNNING" })).status, 200);
    return runId;
  }
  const begin = (day = "2026-10-27", changes = {}, options = {}) => call("delivery.begin", { market_date: day, recipient_hmac: slot, attempt_id: id(), reviewed_retry: false, ...changes }, options);
  const recover = (runId, changes = {}, options = {}) => call("run.recover", { market_date: "2026-10-27", run_id: runId, sender_stopped: true, ...changes }, { admin: true, ...options });
  return { db, adapter, call, run, recipient, claim, begin, recover };
}

async function test(name, body) {
  const f = fixture();
  try { await body(f); passed += 1; console.log("PASS " + name); }
  finally { f.db.close(); }
}

await test("recovery requires admin and positive sender-stop attestation", async f => {
  const runId = await f.claim(); await f.begin();
  assert.equal((await f.recover(runId, {}, { admin: false })).status, 401);
  assert.equal((await f.recover(runId, { sender_stopped: false })).status, 409);
  assert.equal(f.run("2026-10-27").status, "RUNNING");
  assert.equal(f.recipient("2026-10-27").status, "SENDING");
});

await test("wrong run ID and wrong date leave every recipient unchanged", async f => {
  const runId = await f.claim(); await f.begin();
  await f.claim("2026-10-28"); await f.begin("2026-10-28");
  const before = JSON.stringify(f.db.prepare("SELECT * FROM delivery_recipient ORDER BY market_date").all());
  assert.equal((await f.recover(id())).status, 409);
  assert.equal((await f.recover(runId, { market_date: "2026-10-28" })).status, 409);
  assert.equal(JSON.stringify(f.db.prepare("SELECT * FROM delivery_recipient ORDER BY market_date").all()), before);
});

for (const status of ["REPORT_READY", "DELIVERY_IN_PROGRESS", "COMPLETE", "FAILED", "DELIVERY_UNKNOWN", "PARTIAL_DELIVERY"]) {
  await test("recovery rejects " + status + " without recipient changes", async f => {
    const runId = await f.claim(); await f.begin();
    // Seed an inconsistent/legacy state deliberately: rejected recovery must
    // not mutate even this recipient and cannot release the run claim.
    f.db.prepare("UPDATE run_claim SET status=?").run(status);
    const before = JSON.stringify(f.recipient("2026-10-27"));
    assert.equal((await f.recover(runId)).status, 409);
    assert.equal(f.run("2026-10-27").status, status);
    assert.equal(JSON.stringify(f.recipient("2026-10-27")), before);
  });
}

await test("interrupted CLAIMED is terminalized without releasing the date", async f => {
  const runId = await f.claim("2026-10-27", false);
  assert.equal((await f.recover(runId)).status, 200);
  assert.equal(f.run("2026-10-27").status, "FAILED");
  assert.equal((await f.call("run.claim", { market_date: "2026-10-27", run_id: id() })).status, 409);
});

await test("RUNNING recovery atomically protects in-flight and existing UNKNOWN recipients", async f => {
  const runId = await f.claim(); const first = await f.begin();
  const second = await f.begin("2026-10-27", { recipient_hmac: otherSlot });
  await f.call("delivery.outcome", { market_date: "2026-10-27", recipient_hmac: otherSlot, attempt_id: second.data.attempt_id, status: "UNKNOWN", safe_error: "TELEGRAM_TIMEOUT" });
  const result = await f.recover(runId);
  assert.equal(result.status, 200); assert.equal(result.data.status, "DELIVERY_UNKNOWN");
  assert.equal(f.recipient("2026-10-27").status, "UNKNOWN");
  assert.equal(f.recipient("2026-10-27").attempt_id, first.data.attempt_id);
  assert.equal(f.recipient("2026-10-27", otherSlot).safe_error, "TELEGRAM_TIMEOUT");
  assert.equal((await f.recover(runId, {}, { operationId: result.operationId })).data.replayed, true);
});

await test("recovery write failure rolls back run, recipient and operation", async f => {
  const runId = await f.claim(); await f.begin();
  f.db.exec("CREATE TRIGGER injected_failure BEFORE UPDATE ON delivery_recipient BEGIN SELECT RAISE(ABORT,'synthetic'); END");
  const operationId = id();
  assert.equal((await f.recover(runId, {}, { operationId })).status, 503);
  assert.equal(f.run("2026-10-27").status, "RUNNING");
  assert.equal(f.recipient("2026-10-27").status, "SENDING");
  assert.equal(f.db.prepare("SELECT COUNT(*) AS n FROM operations WHERE operation_id=?").get(operationId).n, 0);
});

await test("concurrent recovery admits one transition and no later recipient mutation", async f => {
  const runId = await f.claim(); await f.begin();
  const results = await Promise.all([f.recover(runId), f.recover(runId)]);
  assert.deepEqual(results.map(result => result.status).sort(), [200, 409]);
  assert.equal(f.run("2026-10-27").status, "DELIVERY_UNKNOWN");
  assert.equal(f.recipient("2026-10-27").attempt, 1);
});

await test("delayed recovery cannot touch recipients after its run precondition changes", async f => {
  const runId = await f.claim(); await f.begin();
  f.adapter.beforeBatch = statements => {
    if (statements[0].params[1] === "run.recover") {
      f.adapter.beforeBatch = null;
      f.db.exec("UPDATE run_claim SET status='COMPLETE'");
    }
  };
  assert.equal((await f.recover(runId)).status, 409);
  assert.equal(f.recipient("2026-10-27").status, "SENDING");
});

await test("reviewed retry needs admin, stop confirmation and FAILED_SAFE", async f => {
  await f.claim(); const first = await f.begin();
  await f.call("delivery.outcome", { market_date: "2026-10-27", recipient_hmac: slot, attempt_id: first.data.attempt_id, status: "UNKNOWN" });
  assert.equal((await f.begin("2026-10-27", { reviewed_retry: true, sender_stopped: true })).status, 401);
  assert.equal((await f.begin("2026-10-27", { reviewed_retry: true }, { admin: true })).status, 409);
  assert.equal((await f.begin("2026-10-27", { reviewed_retry: true, sender_stopped: true }, { admin: true })).status, 409);
  assert.equal(f.recipient("2026-10-27").attempt, 1);
});

await test("delayed old-attempt outcome cannot alter reviewed retry", async f => {
  await f.claim(); const first = await f.begin();
  await f.call("delivery.outcome", { market_date: "2026-10-27", recipient_hmac: slot, attempt_id: first.data.attempt_id, status: "UNKNOWN" });
  await f.call("delivery.reconcile", { market_date: "2026-10-27", recipient_hmac: slot, reviewer_hmac: otherSlot, confirmed_sent: false }, { admin: true });
  const second = await f.begin("2026-10-27", { reviewed_retry: true, sender_stopped: true }, { admin: true });
  assert.equal(second.data.authorized, true);
  const before = JSON.stringify(f.recipient("2026-10-27"));
  assert.equal((await f.call("delivery.outcome", { market_date: "2026-10-27", recipient_hmac: slot, attempt_id: first.data.attempt_id, status: "SENT" })).status, 409);
  assert.equal(JSON.stringify(f.recipient("2026-10-27")), before);
  assert.equal((await f.call("delivery.outcome", { market_date: "2026-10-27", recipient_hmac: slot, attempt_id: second.data.attempt_id, status: "SENT" })).status, 200);
});

await test("same-ID begin replay never reauthorizes and concurrent claims have one owner", async f => {
  const claims = await Promise.all(Array.from({ length: 8 }, () => f.call("run.claim", { market_date: "2026-10-27", run_id: id() })));
  assert.equal(claims.filter(result => result.status === 200).length, 1);
  const runId = f.run("2026-10-27").run_id;
  await f.call("run.transition", { market_date: "2026-10-27", run_id: runId, from: "CLAIMED", to: "RUNNING" });
  const attempt = id(), operationId = id();
  const payload = { market_date: "2026-10-27", recipient_hmac: slot, attempt_id: attempt, reviewed_retry: false };
  assert.equal((await f.call("delivery.begin", payload, { operationId })).data.authorized, true);
  assert.equal((await f.call("delivery.begin", payload, { operationId })).data.authorized, false);
  assert.equal(f.recipient("2026-10-27").attempt, 1);
});

await test("closed ownership gates block sends even after the preflight race", async f => {
  await f.claim();
  f.adapter.beforeBatch = statements => {
    if (statements[0].params[1] === "delivery.begin") {
      f.adapter.beforeBatch = null;
      f.db.exec("UPDATE ownership SET cloud_delivery_enabled=0");
    }
  };
  assert.equal((await f.begin()).status, 409);
  assert.equal(f.recipient("2026-10-27"), undefined);
  assert.equal((await f.begin()).status, 403);
});

await test("terminal run does not authorize a fresh ordinary delivery", async f => {
  const runId = await f.claim();
  await f.call("run.transition", { market_date: "2026-10-27", run_id: runId, from: "RUNNING", to: "COMPLETE" });
  assert.equal((await f.begin()).status, 409);
  assert.equal(f.recipient("2026-10-27"), undefined);
});

await test("scoped state read returns attempt status without leaking slots globally", async f => {
  await f.claim(); const first = await f.begin();
  const scoped = await f.call("state.get", { market_date: "2026-10-27", recipient_hmac: slot });
  assert.equal(scoped.data.delivery_recipient.attempt_id, first.data.attempt_id);
  assert.equal(scoped.data.delivery_protocol_version, 2);
  assert.equal(JSON.stringify((await f.call("state.get", {})).data).includes(slot), false);
  assert.equal((await f.call("state.get", { recipient_hmac: slot })).status, 400);
});

console.log(`WORKER_RECOVERY_SAFETY: ${passed} passed; network_calls=0; database=:memory:`);
