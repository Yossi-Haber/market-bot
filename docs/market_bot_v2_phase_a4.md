# Phase A4 — genuine evidence capture and research preparation

Baseline: `f9cd21637e0ee488bfe75df48323c75fb549006c`.
This phase preserves A1–A3 and adds an optional evidence observer plus a research
readiness check. No live financial-provider requests, paid model calls, production
reports, D1 operations, Telegram sends or deployments were performed.

## Work preserved and completed

Before the continuation, provider capture, historical import, readiness integration,
regression tests and the local preparation packages were implemented. Focused tests
had passed and a full offline run was pending. That run finished with 948 passes.
Continuation verified the saved inputs and results, completed this documentation
and the final diff/security review, and guarded two eager metadata expressions so
disabled capture does not inspect optional Finnhub/SEC metadata. Those compatibility
checks warranted the final focused and full runs recorded below. No dataset or
implementation was restarted or replaced.

## Source boundaries and capture

- Marketaux: captures raw `data` descriptions/bodies before date, article-type,
  quality, forecast, entity and causal filters. Cache hits use the same boundary.
- Finnhub: captures Company News responses before normalization/clustering and
  cached candidates with their original raw metadata before subject selection.
- Official feeds: captures original RSS/Atom descriptions and explicit source
  publication times before V1 release/category eligibility checks.
- SEC: records existing submissions metadata and existing verification-cache
  exhibit excerpts. Metadata without a filing body is not invented prose.
- Company/IR and regulator pages: captures a bounded visible-text excerpt from an
  already acquired HTML response. Script/style/head metadata is excluded from text.
  Only an explicit page date can date this excerpt; candidate-date fallback is not
  treated as the page publication date. V1's parser/result is unchanged.
- GDELT/Google fallback: captures available metadata before company relevance
  filtering. GDELT first-seen time is not substituted for publication time. Unresolved
  URLs and metadata-only content remain limitations, not article bodies.
- Existing FMP responses can also be observed if that client was already invoked;
  the observer does not enable FMP, change its limits or issue any request.

`news/evidence_capture.py` is a context-local observer with a disabled default.
Collectors do not import or invoke a model through it. Observer errors log a sanitized
class and cannot change a provider's returned result. `research_v2/capture.py` owns
the opted-in sink: source-field whitelisting, source/window/schema validation,
mechanical deduplication, the existing 100-item evidence cap, and atomic snapshots.
Windows replacement sharing failures get at most four attempts with 350 ms total
backoff; this is file I/O only and does not alter HTTP/provider retries.

The sink preserves original publication representations, original URLs/publishers,
available source text, filing metadata, completeness and coverage limits. Missing
time/body is not synthesized. Credential query parameters and credential-shaped
exports are rejected. Available provider timestamp metadata is retained; historical
retrieval history can be unknown. Query-associated symbols are explicitly not proof
of subject relevance. Capture never marks an event verified.

## New report-only collection support — prepared, not executed

`run_daily.py` accepts `--v2-evidence-output <new-directory>`, or the process variable
`MARKETBOT_V2_EVIDENCE_OUTPUT`. Capture requires `--dry-run` or `--skip-telegram` and
`ENABLE_LLM=0`; otherwise it fails before collection. This prevents the optional
capture path from sending Telegram or invoking V1's paid narrative model.
The context spans the existing driver/news stages, closes in `finally`, and saves
partial evidence after failures. Interrupted collection cannot report readiness.
Default runs do not enable capture, and production algorithms, source tiers,
request counts, paging, quotas, timeouts and classifications are unchanged.

Use this only within a separately authorized report-only execution that retains
the existing quota ownership/D1 controls. In cloud execution, the existing wrapper
can pass the opt-in process variable through to its underlying no-send pipeline.
This phase does not authorize such a run or change workflow configuration.
Do not use a standalone local collection to bypass cloud quota ownership.
Cloud runners are ephemeral: existing production artifact uploads retain only the
six production report files. A future approved cloud capture must also explicitly
retain the private capture files before runner teardown; that upload change was not
made here. Otherwise the original archive gap would recur.

## Historical import and quantitative context

The cache-only CLI reads only explicitly dated Marketaux, Finnhub, verification
and official-feed caches. It never invokes collectors or reuses a newer date.
Source files are hashed and left unchanged. Optional `--families` is an explicit
offline subset selector, not an increase in collection or result limits. Included
and excluded families are recorded. XML is parsed locally using existing parsers.

Saved report JSON is accepted alongside existing production ZIPs. Market data and
all saved mover facts remain unchanged. Existing sector mappings provide a labeled
static peer context; the mapping hash is recorded and its historical classification
date is explicitly unknown. Existing daily returns/sector benchmarks supply the
A1 quantitative context without a new financial-data call. Missing histories,
industry mappings and benchmark data are not fabricated. Co-movement is not causality.

## Readiness and A3 integration

`v2_research_readiness.json` is now the eighth A3 package artifact. Before any paid
transport, readiness checks packet hashes/IDs, explicit timestamps, genuine-input
provenance, usable pre-close mover-associated source text, actual index/sector data,
relevant peer membership, provider/capture failures and token/item budgets.
Marked synthetic inputs and reserved fixture hosts cannot become ready real data.
Hashes provide reproducibility, not cryptographic authentication of a publisher;
capture and operator-supplied archives remain the provenance trust boundary.

Statuses are exactly `READY_FOR_LLM_RESEARCH`, `INSUFFICIENT_SOURCE_EVIDENCE`,
`PROVIDER_COVERAGE_GAP`, or `BUDGET_EXCEEDED`. READY permits a bounded partial
research experiment with disclosed uncovered movers; it never asserts exhaustive
discovery, source independence, causation, publication approval or model/account
availability. Paid execution still requires explicit authorization, credentials,
matching model/pricing and a spending ceiling. A2 validation is unchanged.

## Actual preparation results

The original October 28 report ZIP is genuine but lacks original source snapshots.
No dated October 28 source cache was found locally. All ten saved movers, including
ORCL/MU/LRCX/AMAT/NOW, therefore lack corresponding captured source evidence. The
original October 28 news corpus cannot be reconstructed faithfully from these files.
No source from another date was substituted into it.

| Prepared corpus | Retained source items | Input upper estimate | Readiness |
| --- | ---: | ---: | --- |
| October 28 report plus dated-cache lookup | 0 | 21,751 | PROVIDER_COVERAGE_GAP |
| October 22, all available cache families | 100 | 151,695 | BUDGET_EXCEEDED |
| October 22, explicit Marketaux-only subset | 16 | 48,270 | READY_FOR_LLM_RESEARCH |

The October 22 subset uses 10 existing raw response cache files: 22 returned items,
4 mechanical duplicates and 2 hygiene/window exclusions leave 16 original excerpts.
No full article bodies are present. Source quality/independence remains unverified;
excluded Finnhub/official/verification families are explicit. This is an alternative
historical trial candidate, not October 28 recovery or a complete day reconstruction.

Its timing distribution is 4 pre-market, 7 regular-session and 5 post-close items.
Company associations among the ten actual October 22 movers are:

| Symbol | Associated articles | Qualifying pre-close articles |
| --- | ---: | ---: |
| ACN | 0 | 0 |
| LMT | 0 | 0 |
| CSX | 0 | 0 |
| ADBE | 0 | 0 |
| INTU | 1 | 0 |
| LRCX | 3 | 2 |
| KLAC | 0 | 0 |
| MRVL | 1 | 0 |
| AMAT | 0 | 0 |
| AMD | 0 | 0 |

Only LRCX has qualifying pre-close mover-associated text. Association comes from
provider metadata and is not proof of a stock catalyst. INTU/MRVL's retained articles
are post-close and cannot explain the earlier regular-session moves. The model must
abstain on unsupported cases; A2 remains responsible for quotation and attribution
checks. Broader market articles in the packet do not fill missing company coverage.

## Outputs and reproduction

Outputs are private/ignored under `output/qa/market_bot_v2_phase_a4/`:

- `october_28_preparation/review/`: genuine target packet, coverage/readiness and
  no-LLM review artifacts showing the missing-source blocker.
- `october_22_preparation/review/`: full available import, explicitly over budget.
- `october_22_marketaux_subset/review/`: genuine bounded packet, coverage, readiness,
  no-LLM raw/validated/audit placeholders, English review and usage (zero API calls).
- `october_22_marketaux_subset/capture/v2_captured_recordings.json`: original excerpts,
  scopes, limitations and source hashes, with excluded source families declared.
- `october_22_marketaux_subset/inputs/daily_report_data.json`: byte-for-byte frozen
  report input, hash-checked against the packet's provenance.
- `october_22_marketaux_subset/first_authorized_llm_command.txt`: prepared command;
  no model call was executed.

Source hashes for all imported cache files were verified unchanged after tests.
Earlier incomplete preparation attempts were preserved, not reused as final packages.

To prepare again, choose a new output directory:

```powershell
python -m research_v2.capture --market-date 2026-10-22 --report output/qa/market_bot_v2_phase_a4/october_22_marketaux_subset/inputs/daily_report_data.json --cache-root cache --families marketaux --output output/qa/market_bot_v2_phase_a4/new_october_22_preparation
```

The CLI blocks network/DNS, uses existing files only and performs no LLM call.

## First real model experiment — authorization still required

No new trading date is necessary for a partial October 22 experiment if the operator
explicitly approves that alternative date and limited scope. For an October 28
experiment, genuine October 28 snapshots must first be supplied. If unavailable,
capture a separately approved future trading date; a new date cannot reconstruct
October 28 retrospectively.

Before executing: review the source packet, incomplete bodies and timing limits;
approve the date/scope/model and spending ceiling; supply an existing API credential
through a secure process environment; and verify current model access/pricing.
No key creation/rotation or production setting change is required or performed here.

Configured candidate: `gpt-4.1-mini`, overridable with `--model` plus a matching pricing
file. The [documented model pricing](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
used by `pricing_example.json` is dated October 29, 2026: $0.40/M input and $1.60/M
output tokens. The bounded subset estimates **$0.025708** for its input upper estimate
plus 4,000 output tokens. Caps of 50,000 input and 4,000 output imply **$0.0264** at
these rates. The explicit ceiling below is **$0.03**, one attempted call, zero
automatic retries. These are budgeting estimates, not a provider-enforced billing
guarantee; no charge or model access has been tested in this phase.

Prepared command, **not executed**:

```powershell
python -m research_v2.session --mode real-llm --archive 'output/qa/market_bot_v2_phase_a4/october_22_marketaux_subset/inputs/daily_report_data.json' --market-date 2026-10-22 --recordings 'output/qa/market_bot_v2_phase_a4/october_22_marketaux_subset/capture/v2_captured_recordings.json' --model gpt-4.1-mini --pricing research_v2/config/pricing_example.json --authorize-paid-llm --max-spend-usd 0.03 --max-calls 1 --max-input-tokens 50000 --max-output-tokens 4000 --timeout-seconds 60 --output 'output/qa/market_bot_v2_phase_a4/authorized_october_22_trial'
```

Without a reviewed source-authority policy, A2 keeps unknown independence unverified.
SEMANTIC_REVIEW_REQUIRED claims remain review-only. Nothing is published or sent.

## Changed files and validation

- New observer/parser: `news/evidence_capture.py`.
- Existing passive hook sites: `news/{discovery,marketaux,official,sources,verification_acquisition}.py`
  and `news/providers/{finnhub,fmp}.py`.
- New capture/import and readiness modules: `research_v2/{capture,readiness}.py`.
- Existing V2 integrations: `research_v2/{artifacts,evidence,session}.py`.
- Optional report-only capture switch: `run_daily.py`.
- Regressions: `tests/test_research_v2_a4.py`; A3 artifact-count/integration setup
  updated for the new readiness artifact and explicit synthetic isolation.
- This document. No raw evidence, credentials, generated output or private labels
  are committed.

Focused validation on the final code: **139 passed**. This includes pre-filter
capture, cache/live parity with unchanged request counts, original RSS dates,
unknown dates, SEC/IR excerpts, metadata-only fallback, quotas, hygiene, budgets,
synthetic rejection, paid-call blocking, atomic-write safety and report-only cleanup.
Compilation and Git whitespace checks passed. Final full-suite and staged security
results are recorded after completion below.

Final completed-code results:

- `python -m pytest -q -p tests.offline_network_guard`: **950 passed**, 226.33 seconds.
- Focused V2 and existing daily-orchestrator checks: **139 passed**.
- `python -m compileall -q research_v2 news run_daily.py`: passed.
- Staged scope and secret-pattern scan: passed for the 17 reviewed files.
- Imported cache hashes and the frozen report hash remain unchanged after testing.
- Actual financial-provider requests, paid model requests, Telegram sends and D1
  business-state operations in this phase: **0**.
- Current process credential status: `LLM_CREDENTIALS_UNAVAILABLE`; this does not
  imply that GitHub Secrets or the operator's private credential storage is missing
  a key. An existing key must be supplied securely to the isolated trial process.

All uncommitted implementation progress and private preparation artifacts from
before continuation were preserved. The implementation is ready for review and
explicit authorization of the bounded October 22 alternative experiment; October
28 remains blocked by absent genuine source snapshots.
