# Protocol 2 isolated integration

The production Worker, Python client, SQL schema and business logic are unchanged.
`scripts/protocol2_integration.py` reuses the Phase 3C signing, isolation checks,
synthetic identifiers, credential prompts and operation journal. It has no provider
or Telegram integration. Default execution prints a plan and makes no requests.

## Local execution

From `cloudflare/marketbot-state-worker`, with pinned npm dependencies installed:

```powershell
npm ci
node scripts/local_d1_tests.mjs
node --no-warnings scripts/recovery_safety_tests.mjs
```

The local harness creates a temporary Wrangler configuration, synthetic DB binding,
and disposable persistence directory. Every D1 CLI command requires `--local`;
the HTTP listener is fixed to 127.0.0.1:8787. It launches the Python scenario suite
against the real local Worker using synthetic credentials. The Python transport
rewrites only the pinned TEST endpoint to loopback and refuses redirects. The
harness stops the Worker and removes its temporary database/journal on exit.
Use an available port 8787 and Python 3.10+ on PATH; Node must support node:sqlite
for the separate recovery simulation suite.

The shared Python suite checks schema 1/protocol 2, claim/RUNNING transitions,
attempt authorization and matching success outcome, duplicate and same-operation
replay denial, missing/stale attempt denial, UNKNOWN preservation, admin/stop
requirements, reconciliation plus explicitly reviewed retry, wrong-run rejection,
RUNNING recovery, CLAIMED recovery retaining the date lock, and closed test gates.
It verifies final read-back and integrity. Existing local tests also cover wrong
HMAC, schema rejection, ownership enforcement, concurrency and retention.

Real local D1 failure injection aborts recipient recovery through a SQL trigger;
the test verifies that the run, recipient and operation insert roll back before
removing the trigger and testing successful recovery. In-memory recovery tests
add precondition races, concurrent recovery and delayed old-attempt outcomes.

## Optional remote execution — separate approval required

No remote deployment, identity seeding, mutation, cleanup or remote test was run
as part of this implementation. Local success does not establish remote success.

Prerequisites for the *same Python scenarios* remotely:

1. Independently review and approve deployment of this commit to the existing
   `marketbot-state-api-test` Worker using `wrangler.test.jsonc` only. Production
   config must never be used for this procedure. Preserve existing TEST secrets.
2. TEST binding DB must identify `marketbot-test`, ID
   `8049a22c-eb1f-4e9d-928d-29b35dcc6f70`. Schema remains 1; no migration is added.
3. The existing TEST mutation identity must be present and attested with the
   TEST admin secret. Prior Phase 3C cleanup removed it; restoring it through the
   existing test-identity procedure requires separate explicit approval.
4. Gates remain 0,0,0. The runner verifies TEST configuration and performs the
   existing read-only SQL identity, admin attestation and empty-ledger preflight
   before mutations. The Worker must return test_mutation_mode=true and protocol 2.
5. Supply only independent TEST credentials via the existing hidden prompts or
   MARKETBOT_D1_TEST_HMAC_SECRET / MARKETBOT_D1_TEST_ADMIN_HMAC_SECRET environment
   variables. No production credentials. Wrangler read authentication is also
   required for the TEST SQL preflight.

From the repository root, inspect the plan without credentials/network:

```powershell
python cloudflare/marketbot-state-worker/scripts/protocol2_integration.py
```

Only after separate approval, run:

```powershell
python cloudflare/marketbot-state-worker/scripts/protocol2_integration.py --execute-isolated-remote --database-id 8049a22c-eb1f-4e9d-928d-29b35dcc6f70
```

Only the pinned TEST endpoint is accepted; arbitrary URLs and production DB IDs
are rejected before credentials/networking. Dates 2099-02-01 through 2099-02-03,
3c-prefixed operation/run/attempt IDs and the existing TEST recipient slots are
used. No quota reservations or ownership writes are authorized by this suite.
The ownership-change negative test must be denied by the TEST Worker.

Remote operations append to `output/qa/protocol2_integration/operations.jsonl`;
the journal is flushed and fsynced before sending each mutation and after each
result. It records operation IDs and synthetic attempt context without secrets.
Uncertain responses halt execution; there is no automatic full-suite restart or
cleanup. Preserve the journal and reconcile TEST operation IDs and delivery rows
before any further mutation. A second execution against populated state fails
the empty-ledger preflight. This runner deliberately avoids the historical
Phase 3C full/resume modes and their fixed reconciled October baseline.

The old remote runner remains a historical recovery tool: do not run its legacy
mutation modes against protocol 2. The new entry point reuses its helpers, not
its outdated mutation payloads. No cleanup command is automatically invoked.
Cleanup requires a reviewed inventory of this run's journal and existing scoped
TEST cleanup procedure, preserving unrelated records.

Remote failure injection and race scheduling are not supported by the API.
Those assertions are performed locally against workerd/D1 and in-memory SQL;
remote execution validates normal HTTP/D1 transitions and authorization only.
