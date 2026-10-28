# Analytical correctness corrections (baseline fc64b3c5)

## Evaluation metrics — MB-AUD-010

The product evaluator compares each independent label with the selected event
and final verdict. `CORRECT_CAUSAL` expects `CONFIRMED`, `PLAUSIBLE_CAUSAL`
expects `PLAUSIBLE_NOT_VERIFIED`, and `NO_PUBLIC_CATALYST` expects abstention.
Legacy `FALSE_POSITIVE` / `FALSE_NEGATIVE` labels describe an earlier model and
cannot establish independent truth. They and unclear labels are counted as
excluded; existing human data is never rewritten.

Event identification and causal attribution have separate TP/FP/FN/TN and
positive/negative support counts, precision, and recall. Wrong-event predictions
contribute one FP and one FN. Event identity is compared conservatively using
case/punctuation-normalized `expected_event`; a missing expected event is counted
as unassessed for event identity. Status comparisons remain available for legacy
independent labels without event text. Empty denominators are null, not 100%.
Existing metric keys remain, with corrected definitions and additional counts.

## Session timing — MB-AUD-002

The existing NYSE calendar supplies opening/closing instants, early closes, DST,
holidays and the previous trading close. Eligible publication is strictly after
that previous close and at or before the current close. Day-only publication
dates cannot prove timing and remain unknown. Explicit occurrence timestamps,
earliest supported publication and representative publication remain distinct;
an occurrence time is never invented from publication.

An earlier cluster member can support timing only with direct subject identity
and matching event content. Topic/action, actor, quarter/amount and direction
checks prevent unrelated events and previews from backdating a later event.
When a post-close representative is supported by an earlier report, production
verification receives the earlier title, summary and source, preserving the
representative as provenance rather than importing post-close facts.

The provider query dates and request count are unchanged. The safe window can
validate previous-session after-hours evidence when supplied, but this patch
does not add provider calls or rebuild/widen existing discovery caches. Broader
input acquisition and its cache/quota implications require separate approval.
Old cache records containing only timestamps cannot rescue a future article:
structured same-event evidence is required. Unknown/ambiguous cases abstain.

## Upcoming events — MB-AUD-008

Finnhub classification and the causal gate share deterministic event-language
checks. Future/modal release language, preview context, questions and explicit
future event dates cannot establish an occurred event. A real asserted guidance
cut, earnings release, analyst action or acquisition remains eligible even when
preview language appears in background context. Existing analyst direction
compatibility and verification thresholds remain in force.

## Public relevance and Hebrew — MB-AUD-011

Company stories require a material announcement signal. Official provenance,
tracked membership or price movement alone does not make a routine filing news.
Material mover/tracked events receive explicit relevance reasons; material
announcements outside the curated universe and global macro stories remain
eligible. Verification and editorial-eligibility checks still precede promotion.

Hebrew fields contain supplied Hebrew or narrow deterministic templates, without
new amounts, direction or causal claims. Unsupported translation is explicitly
marked UNTRANSLATED and excluded from Hebrew report selection. Markdown labels
untranslated legacy inputs explicitly if invoked directly. The established
BA/UPS earnings bridge and its evidence gates remain unchanged.
Market-wide announcements without a supported translation retain a generic
Hebrew category notice explicitly saying the original title was not translated;
no policy decision, economic value, or causal implication is invented.

## Validation and operations

Regressions use synthetic records and temporary directories. Run:

```text
python -m pytest -q -p tests.offline_network_guard
python -m compileall -q .
```

No provider quota, D1 business state, credentials, Telegram, scheduling or LLM
enablement is changed. Deployment and production workflow execution are separate
from this code commit. These corrections are verified offline; no new production
report is generated as part of their validation.
