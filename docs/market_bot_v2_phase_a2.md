# V2 Phase A2 evidence validation

Baseline: `0e3c92903e7ef9d71c7c8a20bcdf0d8e90393feb`.
Implementation is isolated under `research_v2`. Production orchestration, V1,
Cloudflare, D1, delivery, scheduling, credentials and authorization are unchanged.

## Contracts and architecture

`contracts.CLAIM_SCHEMA` and `schemas/claim_v2.json` describe individual claims:
claim ID, linked event, claim type, subject, literal predicate, entities, citations
with quotation text, event time, and numeric metric/value/unit where applicable.
Types are EVENT_FACT, NUMERIC_FACT, INTERPRETATION and CAUSAL_ATTRIBUTION.
All fields are required; unknown event time is null and unused numeric fields
are empty strings. IDs are references to packet snapshots, not proof of support.

Research accepts an optional `claims` array for A1 archive compatibility. New
research instructions request it. An A1 event without typed claims produces
SEMANTIC_REVIEW_REQUIRED; its opaque summary cannot become an approved fact.
Malformed claims, duplicate IDs and bad references fail closed with audit reasons.
Invalid top-level research is rejected as a whole. No original schema requirements
were removed. Optional fields are validated whenever present.

`validate_ledger(packet, research, source_policy=...)` first revalidates the packet,
including evidence hashes and NYSE boundaries. It validates every event-level
quotation, then each typed claim. Only `source_text` can supply a quotation:
titles, filing metadata, evidence IDs, entity tags and summaries elsewhere in
metadata cannot substitute for source text. Source summaries deliberately captured
as EXCERPT in A1 remain eligible as quoted reports, never assumed full documents.

Quotation checks normalize whitespace, NFKC Unicode and equivalent quote/dash
punctuation only. Matching is otherwise exact and case-sensitive, not semantic
or fuzzy. The ledger preserves offsets, original excerpts, publisher, original URL
and publication timestamp. A valid quotation alone is insufficient: the predicate
must itself occur verbatim, identify the supplied subject/aliases and entities,
pass numeric checks and the limited contradiction checks. Recognized direct
subject/action and analyst-action forms support literal source reports; other
relationships require semantic review. These are bounded safety checks, not a
replacement news classifier.

Partial sentence quotations require review, and prospective/negating language
in the surrounding paragraph also blocks promotion. This deliberately abstains
on ambiguous excerpts rather than allowing omitted context to reverse a claim.

NUMERIC_FACT checks retain currency and percentage units, normalize equivalent
decimal values, and require the value immediately in its stated metric context.
For example, revenue growth 8% cannot establish earnings 8, revenue 8, or growth 9%.
This phase validates cited passages, not arbitrary unquoted model arithmetic.
INTERPRETATION and CAUSAL_ATTRIBUTION always require semantic review. Prospective,
negated and uncertain language is not treated as completed occurrence. Completed
earnings wording and analyst upgrades have narrow contradiction checks: a date
announcement or reiteration cannot silently become results or an upgrade.

## Source independence and authority

Acquisition may preserve optional `source_provenance` with kind PRIMARY,
INDEPENDENT, SYNDICATED, AGGREGATOR or UNKNOWN, original publisher/URL and issuer
symbols. It is part of the hashed evidence snapshot. It must come from captured
provider/editorial metadata or a reviewed acquisition mapping, never an LLM.
Absent or unverifiable origins do not become independent reporters.

Primary authority additionally requires a separately supplied caller-owned policy
mapping the exact hostname to publisher and authorized subjects. Research output
cannot set it. The synthetic policy fixture authorizes only issuer.example/ORCL.
No production source tiers or authority policy were changed. The policy is not a
cryptographic certificate: acquisition authenticity and policy governance remain
trusted inputs. Claim text cannot establish its own publisher or official status.

Each fact requires both distinct original publishers and distinct hosts, collapsing declared
syndication and identical normalized article bodies. A single authorized primary
source can establish occurrence without a second reporter. Otherwise known
originals yield SINGLE_SOURCE_REPORTED or MULTI_SOURCE_REPORTED; unknown origins
yield UNVERIFIED. Materiality is always NOT_DETERMINED. Event verification uses
the weakest approved fact's verification, not unrelated citations pooled across
a cluster. Syndication with undisclosed or false metadata and lightly rewritten
copies cannot be proven independent deterministically; no independence should be
asserted at acquisition without a basis.

## Timing and movement attribution

The existing NYSE calendar supplies previous close, early closes, holidays and
DST. Each claim must be supported by its own citations. An earlier citation can
establish availability only if it supports that exact predicate. If a separate
event fact is first supported after close, the whole event is ineligible as the
completed regular session's explanation. Post-close facts can remain recorded
event facts without explaining the preceding close. DATE_ONLY/TIME_UNCERTAIN
evidence cannot establish intraday eligibility. Publication time is explicitly
not asserted as event time; explicit model event-time assertions require review
unless rejected immediately for invalid/out-of-window timestamps.

The requested attribution is advisory. Known reported event facts available
before close can yield PLAUSIBLE_CATALYST, never causal proof solely from timing.
SUPPORTED_CATALYST is not automatically granted by this deterministic gate: an
independently validated causal mechanism is not implemented. Weak or unverified
links yield UNEXPLAINED with a reason. Caller-supplied quantitative securities and
peer membership are recomputed with A1's deterministic co-movement component;
a model-created signal flag cannot establish SECTOR_CO_MOVEMENT. That category
reports only observed direction/size, with no inferred cause.

## Output and replay

The validated ledger includes approved/rejected events, approved literal source
claims, rejected/review-required claims, event verification, approved attribution,
source spans, uncertainties, downgrade reasons, coverage and citation/claim audits.
Free-text event summaries and movement rationales remain SEMANTIC_REVIEW_REQUIRED,
and are not forwarded as approved prose. Approved source text is a source report,
not a general entailment verdict. Future editors must consume approved fields
only, not raw rejected claims or the original research response.

```powershell
python -m research_v2.replay --packet tests/fixtures/research_v2/packet_a2.json --response tests/fixtures/research_v2/response_a2.json --source-policy tests/fixtures/research_v2/source_policy_a2.json --output output/qa/market_bot_v2_phase_a2/replay
```

This exports `research_result.json`, `validated_research_ledger.json`,
`validation_audit_trail.json` and `diagnostics.json` under the separate output root.
Inputs are explicitly synthetic. Replay uses only the fake provider with network
connections/DNS blocked. No providers, paid LLMs, D1 or delivery are invoked.

Example: the synthetic source says “ORCL reported earnings and revenue growth of
8%.” Its literal typed claim is approved as a primary source fact using the
synthetic policy. A second claim asserting 9% is rejected with
UNSUPPORTED_NUMERIC_ASSERTION. The requested SUPPORTED_CATALYST becomes
PLAUSIBLE_CATALYST; the event's occurrence is separate from that interpretation.

## Remaining limitations and next phase

Deterministic rules cannot establish arbitrary paraphrase entailment, subtle
negation, sarcasm, indirect ownership relationships, undisclosed syndication,
source honesty or causal impact. Unsupported forms explicitly require semantic
review; strict matching will abstain on some valid paraphrases. No numerical
confidence or universal official-filing materiality is inferred.

Next phase should prepare a human-reviewable research package and review semantic
exceptions against captured real evidence before building the Hebrew editor.
This gate alone does not authorize publication or production integration.

## Changed files and validation

- `research_v2/validation.py`: new claim/source/time/attribution gate and ledger.
- `research_v2/contracts.py`, `evidence.py`, `research.py`, `replay.py`: optional
  claim/provenance contracts, provenance preservation, claim instructions and audit exports.
- `research_v2/schemas/{claim_v2,research_v2,evidence_packet_v2}.json`: exported contracts.
- `tests/test_research_v2_validation.py`: 42 A2 cases, including adversarial references,
  numbers/units, missing context, source independence, chronology and replay.
- `tests/fixtures/research_v2/{packet_a2,response_a2,source_policy_a2}.json`:
  synthetic approved/rejected examples and a separate synthetic authority policy.
- This document records architecture, trust boundaries and limitations.

Final validation on the completed implementation:

- `python -m pytest -q -p tests.offline_network_guard`: **898 passed**, 190.68 seconds.
- Focused A1/A2 tests: **76 passed**.
- `python -m compileall -q research_v2`: passed.
- Synthetic replay: one approved source fact, one rejected invented number,
  PRIMARY_CONFIRMED event and PLAUSIBLE_CATALYST attribution; live requests = 0.
- Staged scope and credential-pattern scan passed. No local credentials, operational
  caches, production reports, manual labels or unrelated files were staged.
