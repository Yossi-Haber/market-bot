"""Run the bounded Live News V3 coverage target without the daily pipeline."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from dotenv import load_dotenv

from debug_catalyst_integration import load_movers_snapshot_for_date
from news.live import MIN_TARGETED_SUBJECT_RELEVANCE, LiveNewsCoverage


BASE_DIR = Path(__file__).resolve().parent


def validate_subject_gate(request_budget: dict) -> tuple[bool, list[dict]]:
    """Validate only candidates explicitly selected for verification."""
    violations = [
        ranked
        for item in request_budget.get("targeted_diagnostics", [])
        for ranked in item.get("candidate_ranking", [])
        if ranked.get("selected_for_verification") is True
        and ranked.get("subject_relevance_score", 0) < MIN_TARGETED_SUBJECT_RELEVANCE
    ]
    return not violations, violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--force-live", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    logger = logging.getLogger("debug_news_coverage")
    load_dotenv(BASE_DIR / ".env")
    try:
        movers = load_movers_snapshot_for_date(BASE_DIR / "output" / "movers_snapshot.json", args.market_date)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc))
        return 1
    details = [item for group in ("top_gainers", "top_losers") for item in movers.get(group, []) if isinstance(item, dict) and item.get("symbol")]
    symbols = list(dict.fromkeys(str(item["symbol"]).upper() for item in details))
    stories, budget = LiveNewsCoverage(logger, args.market_date, perform_verification=True, force_live=args.force_live).collect(symbols=[], mover_symbols=symbols, mover_details=details)
    print(f"verified_before={budget.get('verified_stories_before_escalation', 0)}")
    print(f"verified_after={budget.get('verified_stories_after_escalation', 0)}")
    print(f"coverage_target={budget.get('coverage_target', 2)}")
    print(f"coverage_target_met={budget.get('coverage_target_met', False)}")
    print(f"general_candidates={budget.get('candidates_before_relevance_filter', 0)}")
    print(f"targeted_movers={budget.get('targeted_movers_queried', 0)}")
    print(f"targeted_movers_considered={budget.get('targeted_movers_considered', 0)}")
    print(f"targeted_marketaux_requests={budget.get('targeted_marketaux_requests', 0)}")
    print(f"marketaux_usage_limit_reached={budget.get('marketaux_usage_limit_reached', False)}")
    print(f"marketaux_requests_skipped_usage_limit={budget.get('marketaux_requests_skipped_usage_limit', 0)}")
    print(f"targeted_marketaux_skipped_usage_limit={budget.get('targeted_marketaux_skipped_usage_limit', 0)}")
    print(f"targeted_official_requests={budget.get('targeted_official_requests', 0)}")
    print(f"targeted_verification_requests={budget.get('targeted_verification_requests', 0)}")
    print(f"targeted_cache_hits={budget.get('targeted_cache_hits', 0)}")
    print(f"targeted_cache_misses={budget.get('targeted_cache_misses', 0)}")
    print(f"targeted_marketaux_http_success={budget.get('targeted_marketaux_http_success', 0)}")
    print(f"targeted_marketaux_api_errors={budget.get('targeted_marketaux_api_errors', 0)}")
    print(f"targeted_marketaux_articles_returned={budget.get('targeted_marketaux_articles_returned', 0)}")
    print(f"targeted_entity_matches={budget.get('targeted_entity_matches', 0)}")
    print(f"targeted_entity_missing={budget.get('targeted_entity_missing', 0)}")
    print(f"targeted_candidates_to_verification={budget.get('targeted_candidates_to_verification', 0)}")
    print(f"targeted_tier2_requests={budget.get('targeted_tier2_requests', 0)}")
    print(f"targeted_verification_sources_attempted={budget.get('targeted_verification_sources_attempted', 0)}")
    print(f"targeted_verification_no_source_attempt={budget.get('targeted_verification_no_source_attempt', 0)}")
    print(f"targeted_verification_cache_hits={budget.get('targeted_verification_cache_hits', 0)}")
    print(f"targeted_subject_gate_passed={budget.get('targeted_subject_gate_passed', 0)}")
    print(f"targeted_subject_gate_rejected={budget.get('targeted_subject_gate_rejected', 0)}")
    print(f"targeted_event_driven_candidates={budget.get('targeted_event_driven_candidates', 0)}")
    print(f"targeted_commentary_candidates={budget.get('targeted_commentary_candidates', 0)}")
    print(f"general_marketaux_timeouts={budget.get('general_marketaux_timeouts', 0)}")
    print(f"general_marketaux_timeout_seconds_total={budget.get('general_marketaux_timeout_seconds_total', 0)}")
    print(f"targeted_marketaux_timeouts={budget.get('targeted_marketaux_timeouts', 0)}")
    print(f"coverage_deadline_exhausted={budget.get('coverage_deadline_exhausted', False)}")
    print(f"coverage_requests_skipped_budget={budget.get('coverage_requests_skipped_budget', 0)}")
    print(f"second_candidates_skipped_budget={budget.get('second_candidates_skipped_budget', 0)}")
    print(f"coverage_escalation_started={budget.get('coverage_escalation_started', False)}")
    print("targeted_mover_diagnostics=")
    for item in budget.get("targeted_diagnostics", []):
        print(f"- targeted_symbol={item.get('symbol', '')}")
        print(f"  targeted_query={item.get('query', '')}")
        print(f"  targeted_raw={item.get('raw_results', 0)}")
        print(f"  targeted_after_filter={item.get('after_relevance_filter', 0)}")
        print(f"  targeted_to_verification={item.get('verification_candidates', 0)}")
        print(f"  targeted_rejection_reasons={item.get('rejection_reasons', {})}")
        for gate in item.get("subject_gate_results", []):
            print(f"  subject_gate={gate.get('subject_gate')} subject_relevance_score={gate.get('subject_relevance_score')} subject_gate_reason={gate.get('subject_gate_reason')} headline={gate.get('headline')}")
        for ranked in item.get("candidate_ranking", []):
            print(
                "  candidate_rank={rank} event_type={event_type} selection_score={candidate_selection_score} "
                "subject_relevance_score={subject_relevance_score} event_signal_score={event_signal_score} "
                "verification_likelihood_score={verification_likelihood_score} selected_for_verification={selected_for_verification} "
                "headline={headline}".format(**ranked)
            )
    subject_gate_passed, subject_gate_violations = validate_subject_gate(budget)
    if not subject_gate_passed:
        print("TARGETED_SUBJECT_GATE_VIOLATION")
        return 1
    print("subject_gate_validation=PASS")
    if (
        budget.get("verified_stories_before_escalation", 0) < budget.get("coverage_target", 2)
        and budget.get("coverage_escalation_started")
        and budget.get("targeted_movers_considered", 0) > 0
        and not any(budget.get(key, 0) for key in ("targeted_marketaux_requests", "targeted_official_requests", "targeted_verification_requests", "targeted_cache_hits"))
    ):
        print("COVERAGE_ESCALATION_NO_PROVIDER_REQUESTS")
        return 1
    if (
        budget.get("targeted_candidates_to_verification", 0) > 0
        and budget.get("targeted_verification_requests", 0) > 0
        and budget.get("targeted_verification_sources_attempted", 0) == 0
        and budget.get("targeted_verification_cache_hits", 0) == 0
    ):
        print("TARGETED_VERIFICATION_NO_ACQUISITION")
        return 1
    print(f"targeted_requests={budget.get('targeted_marketaux_requests', 0) + budget.get('targeted_verification_requests', 0)}")
    print(f"escalation_seconds={budget.get('coverage_escalation_seconds', 0)}")
    print(f"total_news_seconds={budget.get('total_news_seconds', 0)}")
    print(f"coverage_status={budget.get('news_coverage_status', 'INSUFFICIENT')}")
    print("verified_story_headlines=")
    for story in stories:
        if story.get("verification") == "VERIFIED" and story.get("editorial_eligible") is True:
            source = story.get("primary_source_name") or story.get("source_name") or "UNKNOWN"
            print(f"- {story.get('title') or story.get('title_he')} | verification_source={source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
