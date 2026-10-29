# D1 recovery and Telegram safety protocol 2

Baseline: a54e6fc6256cfcd98075e4fc8064d479999c811d.
These changes do not deploy the Worker or enable any cloud operation.

## Acknowledgments and uncertainty

A successful photo acknowledgment requires HTTP 2xx, valid object JSON,
`ok: true`, and a message result with a positive integer `message_id` (not a
boolean/string). Invalid JSON, incomplete results, transport loss, redirects
and HTTP 5xx produce UNKNOWN. Only explicit supported Telegram 4xx errors with
`ok: false` and a matching integer `error_code` establish FAILED_SAFE.

There is one photo request per authorized recipient attempt and no automatic
Telegram retry. The shared getUpdates helper does not require a message result.

When delivery.begin has an uncertain outcome, transport is not invoked. When an
outcome write fails, a narrowly scoped authenticated `state.get` may resolve the
same attempt as SENT. SENDING, absent, unavailable or mismatched-attempt state
remains UNKNOWN. An absent row cannot prove an earlier request will never commit.
The delivery manifest preserves UNKNOWN; cloud run disposition also checks D1's
aggregate SENDING/UNKNOWN rows before choosing its terminal status. If D1 is
unavailable, durable finalization may be impossible: the claim remains locked and
the local manifest reports an unknown run outcome. No resend follows.

## Attempt fencing

The existing attempt_id column is used; no SQL schema migration is required.
delivery.begin returns the authorized attempt_id. Python retains an immutable,
string-compatible slot authorization for that specific send, not a mutable
per-recipient pointer. delivery.outcome requires the matching attempt_id and can
update only SENDING. Old/missing attempt IDs cannot update a newer attempt.

The existing state.get operation accepts market_date plus recipient_hmac to read
one opaque slot's status/attempt metadata. Default reads remain aggregated and do
not disclose recipient slots. No raw chat IDs, Telegram payloads or credentials
are stored in these state responses.

## Recovery and reviewed retry

Age is diagnostic only. Worker ISO timestamps ending in Z and explicit offsets
are parsed into aware UTC datetimes on Python 3.10. Existing claims are never
automatically recovered/reclaimed by run_daily_cloud.py, regardless of their age.

An operator must independently establish that the original GitHub job/process
has stopped and cannot resume, including any other sender outside that workflow.
Do not equate a 30-minute timestamp, a paused process or loss of connectivity
with termination. Keep the production workflow's admin secret absent.

run.recover now requires the separate admin HMAC and sender_stopped=true. It
supports only a matching CLAIMED or RUNNING run. The run status is guarded again
inside the SQL batch before recipient changes. Wrong IDs, terminal/disallowed
states and delayed requests whose preconditions changed cannot touch recipients.
The run and its recipients change atomically. Existing UNKNOWN stays protected;
SENDING becomes UNKNOWN; the run becomes DELIVERY_UNKNOWN if either exists,
otherwise FAILED. Recovery never removes the claim or authorizes a send.

UNKNOWN requires explicit administrative reconciliation based on independent
delivery evidence. Neither an HTTP timeout nor a missing acknowledgment proves
non-delivery. If evidence remains inconclusive, keep UNKNOWN.

After confirmed non-delivery, a FAILED_SAFE slot may receive an explicit reviewed
retry only with admin authentication, sender_stopped=true and open ownership
gates. Ordinary application credentials cannot authorize reviewed retry.
SENT/UNKNOWN remain non-sendable; attempt count increases only for an authorized
retry. Ordinary new recipient sends require RUNNING. Reviewed retry can settle an
existing FAILED_SAFE slot under FAILED, DELIVERY_UNKNOWN or PARTIAL_DELIVERY
without reopening or deleting the run claim; it cannot add new recipient slots
to a terminal run or send under COMPLETE.

The stop flag is a privileged operator attestation, not technical proof of remote
process death. The Worker cannot inspect GitHub/laptop process liveness. Attempt
fencing protects state updates; it cannot recall a Telegram request already sent.

## Deployment and compatibility (not executed by this change)

1. Keep delivery approval disabled and use an independently reviewed maintenance
   window with no active sender before upgrading. Do not change any live flags as
   part of these tests or copy admin credentials into production Actions jobs.
2. Deploy the reviewed Worker protocol 2 code to the intended environment, then
   use the matching Python client. The SQL schema remains version 1. Existing
   ownership values, claims, quota reservations and delivery rows must be kept.
3. Validate in the isolated test environment first. Older Worker responses cannot
   authorize the new Python sender without the returned attempt ID. Older outcome
   requests are rejected by the new Worker before recipient mutation.
4. Use state.get to check delivery_protocol_version=2 and the existing ownership
   gates. Any production read/recovery/mutation requires separate approval.
5. Do not blindly reuse historical Phase 3C mutation commands: their older outcome
   payloads and admin/stop requirements must be reviewed for protocol 2 first.
   Existing local Worker tests are updated; this change does not run remote tests.

There is no automatic continuation of a terminal market-date claim. A reviewed
recipient retry does not automatically change the terminal run's status. Existing
business-state inconsistencies are not repaired during deployment. Legacy
send_report.py cannot send without a durable ledger when configured for D1/cloud
persistent delivery; ordinary Windows local mode remains available.

If a terminal DELIVERY_UNKNOWN run still has a SENDING recipient after a state
outage, do not reopen the run or bypass reconciliation. After independently
stopping the original sender, read that exact slot/attempt and use the existing
delivery.outcome operation with its attempt_id to settle SENDING to UNKNOWN.
This authorizes no external action. Then perform administrative reconciliation
only if independent delivery evidence exists. Any such remote procedure needs
explicit approval; this code change performs none of it.

## Offline validation

```text
python -m pytest -q -p tests.offline_network_guard
python -m compileall -q .
node --no-warnings cloudflare/marketbot-state-worker/scripts/recovery_safety_tests.mjs
```

The recovery tests use the real Worker handlers and migration SQL in in-memory
SQLite with synthetic HMAC secrets, no HTTP connection and injected SQL failures.
The existing local_d1_tests.mjs additionally exercises Wrangler/workerd with
--local and a new system-temporary persistence directory. No production D1,
provider API or Telegram is accessed. Report-only execution, market/news/model
logic, provider limits, workflow scheduling and Windows power behavior are not
changed. No claim of exactly-once external Telegram delivery is made.
