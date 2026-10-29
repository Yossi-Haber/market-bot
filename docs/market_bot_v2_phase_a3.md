# V2 Phase A3 — API transport and human research package

Baseline: `b730cce208d47ef04fe70b97ea7f5221a1836aac`.
This is an isolated research workflow. No production entry point imports it.
No financial provider, paid LLM, D1, Telegram or scheduling operation was performed.

## Implementation and compatibility

New files:

- `research_v2/openai_provider.py`: official OpenAI Responses transport, strict
  schema adaptation, explicit authorization, token/call/spending reservations,
  response status/refusal/usage checks and sanitized error reporting.
- `research_v2/artifacts.py`: archived report ZIP reader, optional captured raw
  provider response importer, SHA-256 provenance and per-mover coverage report.
- `research_v2/session.py`: explicit execution modes and seven-artifact English
  review package, using A1 ResearchRunner and the unchanged A2 validation gate.
- `research_v2/config/pricing_example.json`: explicitly dated model/rate example,
  not a default model selection or authorization to spend.
- `tests/test_research_v2_a3.py`: official SDK mocked-HTTP and package regressions.
- This document.

The existing `openai==3.19.2` pin is reused; no production dependencies changed.
The installed SDK supports Responses `text.format` with strict JSON schemas.
API strict schemas require all fields, so the transport makes A2's archive-optional
claims required on the wire and converts constant fields to typed enums. It does
not mutate A1/A2 contracts. ResearchRunner still validates the returned research,
and A2 checks claims before any approved result enters the review document.

Official references checked October 29, 2026:

- [Structured outputs and required fields](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [GPT-4.1 mini support and pricing example](https://developers.openai.com/api/docs/models/gpt-4.1-mini)

No model is selected automatically. Use `--model` or
`MARKETBOT_V2_RESEARCH_MODEL`. The pricing configuration's model must match exactly.
The documented example `gpt-4.1-mini` supports Responses and structured outputs;
account access and billing have not been verified through an API call. Unsupported
models produce an API error, never a substitution. API credits are separate from
a ChatGPT subscription. The current process has no configured OpenAI credential;
real mode reports `LLM_CREDENTIALS_UNAVAILABLE` where applicable.

## Genuine October 28 archive findings

The original archive was found at:
`C:\Users\Yossi Haber\Downloads\marketbot-production-report-40943931803.zip`.

SHA-256: `fc55d822b5e7c943b736e19fdfa7fabd18bb7a68936d4101edb2f121a722d67e`.

The report date is 2026-10-28. Captured moves include ORCL +10.733157%,
MU +5.864806%, LRCX +5.385094%, AMAT +5.069081% and NOW -3.004264%.
The archive has the six original report artifacts, but no original provider/evidence
snapshots. Verified/top stories and unconfirmed leads are empty. Highlight entries
contain quantitative observations, not articles. Local dated cache/output searches
did not find associated October 28 source snapshots.

All five priority movers therefore have 0 available articles and 0 available filings.
Original publication times, source quality and text lengths cannot be measured for
missing documents; missing-body counts are unknown (`null`), not an invented number.
Coverage is `ACQUISITION_GAP_NOT_NO_CATALYST`. This does not establish that no
catalyst existed, nor does it test the real model's reasoning.

Market/mover values, index/macro/sector context and source archive hash are preserved.
V1 causal verdicts, rejected-candidate explanations and translated public story text
are not substituted for original sources. Peer membership or historical indicators
absent from the archive are not inferred. Actual source content is still required
before a meaningful live research test. No synthetic material fills that gap.

## Execution modes and import procedure

All output directories must be new/empty. Existing research packages are preserved.

1. Offline validation: fake structured provider, local packet/response/policy,
   socket connections and DNS blocked; no credentials needed.
2. Real evidence / no LLM: reads an archived ZIP and optional captured source
   responses, exports context and coverage with research `NOT_REQUESTED` and
   validation `NOT_ASSESSED`. Network is blocked throughout.
3. Real evidence / real LLM: requires `--authorize-paid-llm`, an explicit model,
   matching pricing, spending ceiling, API credential and usable source text.
   Missing prerequisites are recorded as BLOCKED with zero calls. Merely selecting
   this mode does not authorize spending. No automatic repair/retry/fallback occurs.

```powershell
python -m research_v2.session --mode offline --packet tests/fixtures/research_v2/packet_a2.json --response tests/fixtures/research_v2/response_a2.json --source-policy tests/fixtures/research_v2/source_policy_a2.json --output output/qa/market_bot_v2_phase_a3/new_offline_review

python -m research_v2.session --mode real-evidence --archive 'C:\Users\Yossi Haber\Downloads\marketbot-production-report-40943931803.zip' --market-date 2026-10-28 --model gpt-4.1-mini --pricing research_v2/config/pricing_example.json --output output/qa/market_bot_v2_phase_a3/new_oct28_coverage
```

For another original production ZIP, supply its path with `--archive`; no extraction
is needed. The loader reads one unambiguous bounded `daily_report_data.json` member,
checks its date and hashes the ZIP. It rejects explicitly marked synthetic reports.
Report authenticity is based on the operator-supplied archive and recorded hash,
not proof from a filename alone.

To import **existing captured** source responses, create a local file outside Git:

```json
{
  "origin": "ARCHIVED_PROVIDER_RESPONSES",
  "recordings": [
    {"provider": "finnhub", "coverage_state": "AVAILABLE", "payload": []}
  ]
}
```

Replace the empty payload with actual retained Finnhub arrays, or use provider
`marketaux` with its original `{"data": [...]}` response. The file is supplied via
`--recordings C:\private\oct28_recordings.json`. Preserve actual source times,
symbols, URLs, titles and available original body/summary text. Do not expand a
summary into an article or attach invented provenance. The importer hashes this
file too. Provider-specific failure/quota states and limitations can be recorded
using A1 coverage states. Source independence/primary authority remains governed
by A2's captured provenance and separate caller-owned `--source-policy` file.
No live source collection is added by this command.

## Generated packages and interpretation

The genuine no-LLM package is under
`output/qa/market_bot_v2_phase_a3/october_28_real_evidence/`.
The separately labeled simulated package is under
`output/qa/market_bot_v2_phase_a3/offline_validation/`.
Each contains:

1. `v2_evidence_packet.json`
2. `v2_evidence_coverage.json`
3. `v2_raw_research_ledger.json`
4. `v2_validated_research_ledger.json`
5. `v2_validation_audit.json`
6. `v2_research_review.md`
7. `v2_llm_usage.json`

Acquisition and coverage are frozen before a possible paid request. The raw model
text is retained for audit even on a local schema failure; SDK errors/headers are
not serialized. Review sections distinguish proposals, approved source facts,
approved attributions, rejected/review-required claims, source quotes, alternatives,
sector context, unexplained/not-assessed moves and acquisition gaps. No-LLM empty
results mean NOT_ASSESSED, not that no financial events occurred. A2 semantic-review
flags never become publication approval. Credential-shaped fields, known token
patterns and the configured API key are blocked from exports.

## Limits and cost accounting

The transport pins `https://api.openai.com/v1`, disables storage and truncation,
and configures finite SDK timeout with `max_retries=0`. Zero automatic retries is
the bounded policy: ambiguous outcomes must not cause hidden extra paid calls.
Default limits are 1 call, 50,000 input-token upper estimate, 4,000 output tokens
and 60 seconds. A1 also bounds its invocation. Truncated/refused/failed responses,
missing usage, excess reported usage, bad references and schema violations fail
closed. No alternative model or V1 classifier is invoked.

Before each attempted call, a lock reserves a conservative input-byte/framing
estimate plus the output cap against explicitly supplied USD token rates and the
spending ceiling. Actual usage/cost estimates, requested/returned models and
latency are logged separately. Unknown outcomes retain the full reservation;
there is no automatic refund or retry. Reservations are per process/run, not a
new durable account quota ledger. Re-running a command requires fresh operator
review of prior uncertain outcomes. Provider-side monthly budgets remain separate.

Example documented rates: $0.40/M input and $1.60/M output tokens, dated above.
For the current **source-empty** real packet, the input upper estimate is 15,973
tokens and the single-call estimate with 4,000 output tokens is **$0.0127892**.
That is a budgeting illustration, not a meaningful research run or actual charge.
The default token caps imply **$0.0264** per attempted call at these example rates.
Captured evidence will change the estimate; the no-LLM mode can recompute it first.
These are conservative estimates, not a provider-enforced billing guarantee: rates,
tokenizer/framing behavior and account terms must be verified before authorization.
No cached-input discount or fixed monthly price is assumed.

## First authorized live test — prerequisites and command

No live request was made. Required next actions:

1. Import the genuine source recordings and run the no-LLM coverage mode. Review
   per-mover gaps and spending estimate. No new financial-provider acquisition is
   authorized by this implementation.
2. Select an available model, verify its current rates/account credits, and supply
   a matching pricing file. Establish the caller-owned source policy where known.
3. Explicitly approve that model, evidence scope and maximum spend. Supply only a
   process-scoped `OPENAI_API_KEY` through a password manager or secure prompt.
4. Only after approval, execute the following against the reviewed inputs:

```powershell
python -m research_v2.session --mode real-llm --archive 'C:\Users\Yossi Haber\Downloads\marketbot-production-report-40943931803.zip' --market-date 2026-10-28 --recordings 'C:\private\oct28_recordings.json' --source-policy 'C:\private\oct28_source_policy.json' --model gpt-4.1-mini --pricing research_v2/config/pricing_example.json --authorize-paid-llm --max-spend-usd 0.03 --max-calls 1 --max-input-tokens 50000 --max-output-tokens 4000 --timeout-seconds 60 --output output/qa/market_bot_v2_phase_a3/authorized_oct28_research
```

This command is prepared only; it has not been executed. Do not paste an API key
into command arguments, source, Git or shell history. The CLI does not auto-load
production `.env`. If using PowerShell, enter via `Read-Host -AsSecureString`, marshal
to the child process environment only, clear the environment afterwards and zero
the BSTR in a `finally` block. No Windows or GitHub configuration change is needed
for this isolated local test. Missing source text or credentials remains a blocker.

## Validation results

- Full offline suite: `python -m pytest -q -p tests.offline_network_guard`:
  **919 passed**, 234.51 seconds, on the completed code.
- Focused V2 suite: **97 passed**, including **21 new A3 cases**.
- `python -m compileall -q research_v2`: passed.
- Official SDK tests use HTTPX2 MockTransport exclusively: structured responses,
  incomplete/refused output, schema rejection, no hidden retry on timeout/429,
  unavailable credentials, input/output/request/spending caps, usage, A2 integration
  and complete package export are covered without a live API request.
- Local archive/review tests cover dates, explicit synthetic rejection, original
  excerpt preservation, missing bodies, no-LLM mode, network isolation, credential
  export blocking and preservation of existing output directories.
- Genuine October 28 no-LLM package: seven files; all JSON parses; API calls = 0.
- Separate synthetic offline package: seven files; all JSON parses; API calls = 0.
- Staged scope/secret-pattern scan and Git whitespace checks passed. Only the six
  source/config/test/documentation files above are committed; local archives,
  generated packages, credentials and unrelated local files remain outside Git.

Implementation is ready for operator review of captured evidence and a separately
authorized API test. Actual October 28 LLM reasoning and account access remain
unvalidated; no research findings are fabricated to stand in for that test.
