# Market Bot V2 Phase A1

Baseline: `a8f1690b2d5e61064b9cb3928533eb85df6b4744`.
This package is isolated from V1: no production entry point imports it.

## Compatibility and reuse

The existing NYSE calendar (`market_calendar.get_session`) supplies actual
opens, closes, holidays and early closes. Existing market/mover dictionaries
can be supplied unchanged as packet context. `NewsCandidate.as_dict()` results,
raw Finnhub Company News arrays, raw Marketaux `data` arrays and recorded official
evidence dictionaries feed the same adapter. No production client, quota, cache,
classifier, D1, Telegram or scheduling code changes.

V1 Marketaux `search` filters dates, source quality, forecast and article types
before returning candidates. Finnhub candidates carry V1 classification metadata.
V2 ignores classification metadata and adapts raw captured responses where
available. Feeding only V1 filtered candidates cannot recover excluded articles;
this is an acquisition gap, not evidence of absent catalysts. No V2 live fetch
or SEC crawler is enabled. Existing provider clients remain the future transport
boundary; V1 filtered `search` must not become the sole V2 acquisition boundary.

## Evidence packet

`research_v2/evidence.py` builds packet version 2, preserving original URL,
publication representation, publisher, provider, title, available original text,
retrieval timestamp, symbols, filing type, SEC items/exhibits/excerpts, completeness,
provider limitations and acquisition provenance. Machine-readable contracts are
under `research_v2/schemas/`. Python validation uses the same schema definitions.
IDs hash the complete evidence snapshot; validation detects content changes.
Missing text remains empty/TITLE_ONLY. Summaries received from providers remain
EXCERPT, never assumed full text. No LLM summary replaces source text.

URL hygiene removes tracking parameters and fragments for deduplication while
preserving original URLs. URLs containing credential query parameters are rejected.
Source identity, timestamps and an item cap are enforced; explicitly supplied spam
URLs can be excluded. Exact normalized URL/title/publication duplicates are removed.
There are no forecast, event-category, ticker-subject or causal filters.

The window starts at the previous NYSE close; prior after-hours, current premarket,
regular session and current post-close evidence are tagged separately. Current
post-close ends at local midnight; later evidence is excluded. Publication timestamps
must include a timezone. Coverage keeps provider state and counts for accepted,
structurally excluded, incomplete and budget-excluded evidence separately. Providers
must explicitly report unavailable/quota/budget states rather than imply no catalyst.

Existing SEC cache records use `filing_form`, `filing_items`, `source_domain`
and `exhibit_text`; these are supported without changing the SEC collector.
Midnight timestamps synthesized from a filing date are explicitly DATE_ONLY /
TIME_UNCERTAIN, with a provider limitation. They cannot establish intraday timing.

## Quantitative context

`peer_context` uses supplied daily returns, sector/subindustry benchmarks, group
membership and prior-session return/volume arrays only. Relative returns are
percentage-point differences. Historical metrics require 20 observations; missing
metrics are null. Rolling volatility uses population standard deviation; anomaly
is `(current_return - historical_mean) / historical_volatility`.
Historical arrays must exclude current/future sessions; this phase does not acquire
or independently establish their chronology. Co-movement means at least three
known group members moving at least 3% in the same direction; it asserts no cause.
These research-context thresholds do not affect production decisions.

## English research contract and provider boundary

`StructuredProvider.complete` receives explicit model, instructions, packet, strict
JSON schema, output-token cap and timeout. No model identifier is assumed or
substituted. A concrete authenticated paid provider adapter is not enabled in A1;
the interface is transport-independent. A non-fake provider without explicitly
available credentials returns UNAVAILABLE, without making a call. Deployments
must validate actual credentials and model support when adding that adapter.
There is no ChatGPT-subscription billing assumption.

`ResearchRunner` enforces request count, UTF-8 input-byte cap and a conservative
total token reservation (input bytes including schema/instructions + output cap).
It checks completion status and actual usage, handles timeout, and never falls
back to V1 or another model. A bounded daemon invocation returns on timeout and
exhausts the runner's request allowance so another request cannot overlap it.
An injected transport must additionally honor its supplied HTTP timeout: Python
cannot forcibly cancel an arbitrary running provider thread or revoke a paid
request that already reached a service. No automatic retry occurs.

Research contains separate canonical events and movement assessments; all fields
are required. Output records have `proposal_only=true`. Evidence IDs and event
references must exist; quotations must carry evidence references and nonempty text.
English instructions require verbatim quotations, uncertainty and separate event
verification and attribution. Exact quote matching, factual entailment, temporal
causal enforcement, source trust gates and publication approval belong to A2;
a valid A1 reference does not establish claim support. No confidence probabilities
or Hebrew editorial generation is implemented.

## Offline replay

```powershell
python -m research_v2.replay --packet tests/fixtures/research_v2/packet.json --response tests/fixtures/research_v2/response.json --output output/qa/market_bot_v2_phase_a1/replay
```

The fixture is explicitly synthetic, not an archived October 28 report. Checked
local report files were dated October 22 and 27; no October 28 dated files were
found under local output/cache. Missing October 28 original provider bodies are
not reconstructed. The replay uses only FakeProvider, blocks socket connections
and DNS within replay, requires separate output directories, and saves research,
simulated usage and diagnostics. It imports no production orchestration/delivery.

## Quotas and next phase

A1 adds zero financial-provider requests, SEC requests, paid LLM calls or delivery
calls. Existing Marketaux limits, request budgets and quota ledgers are unchanged.
The 100-item default is an offline evidence cap, not a provider-result limit increase.
Live raw response capture, provider-specific coverage reconciliation, validated
historical time series, and a concrete structured LLM transport remain gaps.
No request/credit increase is proposed or authorized here. Estimate those costs
from measured raw-result coverage before authorizing live acquisition.

Next phase: implement the deterministic evidence/quotation and temporal attribution
gate with adversarial fixtures before considering publication or Hebrew editing.

## Validation

- Complete offline run: `python -m pytest -q -p tests.offline_network_guard`:
  **854 passed** in 231.16 seconds (822 existing tests and 32 initially collected V2 tests).
- Final focused run after adding content-identity and existing-SEC-shape checks:
  **34 passed**. The two additional checks were added after the full run collected
  its tests; the final V2 modules were verified in the focused run.
- `python -m compileall -q research_v2`: passed.
- Saved synthetic replay: `V2_REPLAY=PROPOSAL`; diagnostics record zero live requests
  and zero delivery calls, with simulated model usage.
- Staged content reviewed: only the new package, schemas, synthetic fixtures,
  tests and this documentation. Credential pattern scan passed. No private caches,
  reports, human labels or unrelated local files are included.
