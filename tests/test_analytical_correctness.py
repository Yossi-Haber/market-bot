"""Synthetic, offline regressions for the four approved audit corrections."""

import json

import pytest

from drivers.causal import evaluate_causal_eligibility, publication_time_relation, session_evidence_window
from drivers.runtime import _candidate_from_cluster, _select_cluster
from evaluate_market_drivers_product import _label_metrics
from news.editorial import build_public_stories, public_hebrew_copy, public_story_relevance
from news.providers.finnhub import classify_finnhub_event, cluster_finnhub_candidates
from news.schema import NewsCandidate
from report.composer import select_verified_live_stories
from render.markdown import render_verified_stories


def metrics(status, label, selected=None, expected="Boeing reports Q3 earnings"):
    return _label_metrics(
        [{"date": "2026-10-27", "symbol": "BA", "final_driver_status": status, "selected_event": selected}],
        [{"date": "2026-10-27", "symbol": "BA", "label": label, "expected_event": expected}],
    )


@pytest.mark.parametrize("status,label,selected,counts", [
    ("CONFIRMED", "CORRECT_CAUSAL", "Boeing reports Q3 earnings", (1, 0, 0, 0)),
    ("NO_CATALYST_FOUND", "CORRECT_CAUSAL", None, (0, 0, 1, 0)),
    ("CONFIRMED", "CORRECT_CAUSAL", "Boeing announces aircraft order", (0, 1, 1, 0)),
    ("NO_CATALYST_FOUND", "NO_PUBLIC_CATALYST", None, (0, 0, 0, 1)),
    ("CONFIRMED", "NO_PUBLIC_CATALYST", "Invented acquisition", (0, 1, 0, 0)),
])
def test_confusion_matrix_compares_truth_and_event(status, label, selected, counts):
    result = metrics(status, label, selected, expected="" if label == "NO_PUBLIC_CATALYST" else "Boeing reports Q3 earnings")
    for key in ("event_identification", "causal_attribution"):
        assert tuple(result[key][name] for name in ("tp", "fp", "fn", "tn")) == counts
        assert result[key]["support"] == 1
    if status == "NO_CATALYST_FOUND" and label == "CORRECT_CAUSAL":
        assert result["correct_records"] == 0
        assert result["recall_on_labeled"] == 0


def test_identifying_event_does_not_establish_causal_attribution():
    result = metrics("PLAUSIBLE_NOT_VERIFIED", "CORRECT_CAUSAL", "Boeing reports Q3 earnings")
    assert result["event_identification"]["tp"] == 1
    assert result["causal_attribution"]["tp"] == 0
    assert result["correct_records"] == 0


def test_empty_and_relative_labels_do_not_create_accuracy():
    empty = _label_metrics([], [])
    assert empty["labeled_records"] == 0
    assert empty["accuracy_on_labeled"] is None
    assert empty["precision_on_labeled"] is None
    assert empty["recall_on_labeled"] is None
    relative = metrics("CONFIRMED", "FALSE_POSITIVE", "Boeing reports Q3 earnings")
    assert relative["excluded_label_records"] == 1
    assert relative["labeled_records"] == 0


def test_metric_denominators_and_inputs_are_preserved():
    labels = [{"date": "2026-10-27", "symbol": symbol, "label": label, "expected_event": event}
              for symbol, label, event in [("BA", "CORRECT_CAUSAL", "Boeing reports earnings"), ("UPS", "CORRECT_CAUSAL", "UPS reports earnings"), ("T", "NO_PUBLIC_CATALYST", ""), ("VZ", "NO_PUBLIC_CATALYST", "")]]
    records = [{"date": "2026-10-27", "symbol": symbol, "final_driver_status": status, "selected_event": event}
               for symbol, status, event in [("BA", "CONFIRMED", "Boeing reports earnings"), ("UPS", "NO_CATALYST_FOUND", None), ("T", "CONFIRMED", "Invented event"), ("VZ", "NO_CATALYST_FOUND", None)]]
    original = json.dumps([records, labels], sort_keys=True)
    result = _label_metrics(records, labels)
    for key in ("event_identification", "causal_attribution"):
        assert result[key]["precision"] == result[key]["recall"] == 0.5
        assert result[key]["positive_support"] == result[key]["negative_support"] == 2
        assert result[key]["support"] == 4
    assert json.dumps([records, labels], sort_keys=True) == original


def test_legacy_label_without_event_does_not_claim_identity_verified():
    result = metrics("PLAUSIBLE_NOT_VERIFIED", "PLAUSIBLE_CAUSAL", expected="")
    assert result["precision_on_labeled"] == 1
    assert result["event_identity_unassessed"] == 1
    assert result["event_identification"]["support"] == 0


def article(title, published, summary=None):
    return NewsCandidate(title, summary, published, "Synthetic Reuters", "example.test", 3, "https://example.test/article", "EARNINGS", ["BA"], "finnhub")


def test_premarket_earnings_and_after_close_recap_use_supported_publication():
    early = article("Boeing reports Q3 earnings", "2026-10-27T07:00:00-04:00", "Earlier public results.")
    late = article("Boeing Q3 earnings recap", "2026-10-27T17:00:00-04:00", "Late recap must not supply new claims.")
    # Deliberately make the recap representative to exercise this failure mode.
    cluster = cluster_finnhub_candidates([late, early], "BA", "Boeing")[0]
    cluster.update(best_candidate=late, canonical_event_title=late.title)
    candidate = _candidate_from_cluster(cluster, "BA", "2026-10-27")
    result = evaluate_causal_eligibility(candidate, "2026-10-27")
    assert result["causal_eligible"] is True
    assert result["earliest_supported_publication_at"] == early.published_at
    assert result["representative_published_at"] == late.published_at
    assert result["event_occurred_at"] is None  # Never invented from publication.
    assert candidate["title"] == early.title
    assert candidate["summary"] == early.summary


@pytest.mark.parametrize("published,market_date,expected", [
    ("2026-10-26T16:30:00-04:00", "2026-10-27", "BEFORE_OR_DURING_SESSION"),
    ("2026-10-27T11:00:00-04:00", "2026-10-27", "BEFORE_OR_DURING_SESSION"),
    ("2026-10-27T16:01:00-04:00", "2026-10-27", "FUTURE_EVENT"),
    ("2026-10-26T16:00:00-04:00", "2026-10-27", "BEFORE_EVIDENCE_WINDOW"),
    ("2026-11-27T12:59:00-05:00", "2026-11-27", "BEFORE_OR_DURING_SESSION"),
    ("2026-11-27T13:01:00-05:00", "2026-11-27", "FUTURE_EVENT"),
    ("2026-10-30T16:30:00-04:00", "2026-11-02", "BEFORE_OR_DURING_SESSION"),
    ("2026-09-04T17:00:00-04:00", "2026-09-08", "BEFORE_OR_DURING_SESSION"),
    ("2026-10-27", "2026-10-27", "UNKNOWN"),
])
def test_calendar_session_window(published, market_date, expected):
    assert publication_time_relation(published, market_date) == expected


def test_dst_window_has_distinct_offsets_and_actual_open_close():
    window = session_evidence_window("2026-11-02")
    assert window["window_start"] == "2026-10-30T16:00:00-04:00"
    assert window["session_open"] == "2026-11-02T09:30:00-05:00"
    assert window["session_close"] == "2026-11-02T16:00:00-05:00"


def test_unrelated_or_different_quarter_member_cannot_backdate_event():
    early = article("Boeing reports Q2 earnings", "2026-10-27T07:00:00-04:00")
    late = article("Boeing reports Q3 earnings", "2026-10-27T17:00:00-04:00")
    cluster = cluster_finnhub_candidates([late], "BA", "Boeing")[0]
    cluster["articles"].append(early)  # Intentionally contaminated input.
    result = evaluate_causal_eligibility(_candidate_from_cluster(cluster, "BA"), "2026-10-27")
    assert result["event_time_relation"] == "FUTURE_EVENT"
    assert result["causal_eligible"] is False
    early.title = "Boeing announces aircraft acquisition"
    assert evaluate_causal_eligibility(_candidate_from_cluster(cluster, "BA"), "2026-10-27")["causal_eligible"] is False


def test_preview_member_cannot_backdate_actual_announcement():
    early = article("Boeing Q3 earnings preview", "2026-10-27T07:00:00-04:00")
    late = article("Boeing reports Q3 earnings", "2026-10-27T17:00:00-04:00")
    cluster = cluster_finnhub_candidates([late], "BA", "Boeing")[0]
    cluster["articles"].append(early)
    assert evaluate_causal_eligibility(_candidate_from_cluster(cluster, "BA", "2026-10-27"), "2026-10-27")["causal_eligible"] is False


def test_generic_identical_headlines_do_not_prove_same_event():
    early = article("Boeing update", "2026-10-27T07:00:00-04:00", "Earlier event.")
    late = article("Boeing update", "2026-10-27T17:00:00-04:00", "Later unrelated event.")
    cluster = cluster_finnhub_candidates([late], "BA", "Boeing")[0]
    cluster["articles"].append(early)
    assert evaluate_causal_eligibility(_candidate_from_cluster(cluster, "BA", "2026-10-27"), "2026-10-27")["causal_eligible"] is False


def test_future_cluster_cannot_hide_timely_unrelated_event():
    early = article("Boeing reports Q2 earnings", "2026-10-27T07:00:00-04:00")
    late = article("Boeing reports Q3 earnings", "2026-10-27T17:00:00-04:00")
    future_cluster = cluster_finnhub_candidates([late], "BA", "Boeing")[0]
    timely_cluster = cluster_finnhub_candidates([early], "BA", "Boeing")[0]
    assert _select_cluster([future_cluster, timely_cluster], "BA", "2026-10-27", 5) is timely_cluster


@pytest.mark.parametrize("title", [
    "What to Expect From Next Week's Earnings",
    "Boeing Will Report Earnings Tomorrow",
    "Boeing Earnings Preview",
    "Boeing Ahead of Quarterly Results",
    "Will Boeing Cut Guidance After Earnings?",
])
def test_previews_cannot_be_rescued_by_event_type_or_summary(title):
    result = evaluate_causal_eligibility({"title": title, "summary": "earnings revenue profit guidance", "published_at": "2026-10-27T10:00:00-04:00", "event_type": "EARNINGS_RESULTS", "event_actions": ["EARNINGS"], "subject_score": 80}, "2026-10-27")
    assert result["causal_eligible"] is False
    assert result["causal_reason"] == "FUTURE_EVENT_PREVIEW"
    assert classify_finnhub_event(title)[0] == "OTHER"


@pytest.mark.parametrize("title", [
    "Boeing Reports Quarterly Earnings",
    "Boeing Cuts Full-Year Guidance",
    "Analyst Downgrades Boeing",
    "Boeing Announces Acquisition",
    "Boeing Cuts Guidance Ahead of Quarterly Results",
])
def test_actual_events_survive_preview_filter(title):
    result = evaluate_causal_eligibility({"title": title, "summary": "Ahead of next week's earnings.", "published_at": "2026-10-27T10:00:00-04:00", "event_type": "EARNINGS_RESULTS", "event_actions": [], "subject_score": 80}, "2026-10-27")
    assert result["causal_eligible"] is True
    assert classify_finnhub_event(title)[0] != "OTHER"


def test_future_explicit_occurrence_cannot_be_backdated_to_publication():
    result = evaluate_causal_eligibility({"title": "Boeing reports earnings", "published_at": "2026-10-27T10:00:00-04:00", "event_date": "2026-10-28", "event_type": "EARNINGS", "subject_score": 80}, "2026-10-27")
    assert result["event_time_relation"] == "FUTURE_EVENT"
    assert result["causal_eligible"] is False


def test_event_occurrence_offset_is_compared_as_an_instant():
    result = evaluate_causal_eligibility({"title": "Boeing reports earnings", "published_at": "2026-10-27T10:00:00-04:00", "event_occurred_at": "2026-10-28T01:00:00+14:00", "event_type": "EARNINGS", "subject_score": 80}, "2026-10-27")
    assert result["causal_eligible"] is True


def test_old_occurrence_does_not_explain_new_session_recap():
    result = evaluate_causal_eligibility({"title": "Boeing reports earnings", "published_at": "2026-10-27T10:00:00-04:00", "event_occurred_at": "2026-10-20T09:00:00-04:00", "event_type": "EARNINGS", "subject_score": 80}, "2026-10-27")
    assert result["event_time_relation"] == "BEFORE_EVIDENCE_WINDOW"
    assert result["causal_eligible"] is False


def public_story(**changes):
    return {"title": "NRIM filed Form 4 ownership disclosure", "summary": "Routine filing", "category": "COMPANY", "affected_symbols": ["NRIM"], "published_at": "2026-10-27T10:00:00-04:00", "verification": "VERIFIED", "editorial_eligible": True, "primary_source_name": "SEC EDGAR", **changes}


def test_routine_filing_is_not_promoted_even_from_official_source(tmp_path):
    path = tmp_path / "news.json"
    path.write_text(json.dumps({"market_date": "2026-10-27", "stories": [public_story()]}), encoding="utf-8")
    assert select_verified_live_stories(path, "2026-10-27") == []
    assert public_story_relevance(public_story(affected_symbols=["BA"]), tracked_symbols={"BA"}) is None


def test_material_company_and_macro_stories_survive_with_hebrew_copy(tmp_path):
    stories = [public_story(title="Boeing reports quarterly earnings", category="EARNINGS", affected_symbols=["BA"]),
               public_story(title="KLXE Raises Q3 Revenue Guidance", category="EARNINGS", affected_symbols=["KLXE"]),
               public_story(title="BLS released inflation report", category="MACRO_RELEASE", affected_symbols=[])]
    path = tmp_path / "news.json"
    path.write_text(json.dumps({"market_date": "2026-10-27", "stories": stories}), encoding="utf-8")
    selected = select_verified_live_stories(path, "2026-10-27")
    assert len(selected) == 3
    assert selected[0]["relevance_reason"] == "TRACKED_MATERIAL_EVENT"
    assert "KLXE העלתה את תחזית ההכנסות לרבעון השלישי" == selected[1]["title_he"]
    assert selected[2]["relevance_reason"] == "MARKET_WIDE_EVENT"
    assert all("אומת" in item["summary_he"] or "פורסם" in item["summary_he"] for item in selected)


def test_untranslated_fields_remain_explicit_and_markdown_labels_them():
    story = public_story(title="Unexpected corporate development", title_he="Unexpected corporate development", summary_he="English summary")
    copy = public_hebrew_copy(story)
    assert copy == {"title_he": None, "summary_he": None, "translation_status": "UNTRANSLATED"}
    rendered = "\n".join(render_verified_stories({"verified_stories": [story]}))
    assert "כותרת (לא תורגמה): Unexpected corporate development" in rendered
    assert "כותרת: Unexpected corporate development" not in rendered


def test_marketwide_statement_without_translation_is_not_silently_dropped(tmp_path):
    story = public_story(title="Federal Reserve publishes monetary policy statement", category="CENTRAL_BANK", affected_symbols=[])
    path = tmp_path / "macro.json"
    path.write_text(json.dumps({"market_date": "2026-10-27", "stories": [story]}), encoding="utf-8")
    selected = select_verified_live_stories(path, "2026-10-27")
    assert len(selected) == 1
    assert selected[0]["title_he"] == "פורסמה הודעה של בנק מרכזי"
    assert "כותרת המקור לא תורגמה" in selected[0]["summary_he"]
    assert story["title"] not in selected[0]["title_he"]


def test_relevant_material_filing_retains_supplied_hebrew(tmp_path):
    story = public_story(title="Boeing Announces Acquisition of Example", category="FILING", affected_symbols=["BA"], title_he="BA הודיעה על רכישה", summary_he="פורסמה הודעה על העסקה.")
    path = tmp_path / "filing.json"
    path.write_text(json.dumps({"market_date": "2026-10-27", "stories": [story]}), encoding="utf-8")
    selected = select_verified_live_stories(path, "2026-10-27")
    assert len(selected) == 1
    assert selected[0]["title_he"] == story["title_he"]


def test_supplied_hebrew_is_preserved():
    story = public_story(title="Verified earnings", title_he="BA פרסמה תוצאות", summary_he="החברה פרסמה את הדוח.")
    copy = public_hebrew_copy(story)
    assert copy["title_he"] == story["title_he"]
    assert copy["summary_he"] == story["summary_he"]
