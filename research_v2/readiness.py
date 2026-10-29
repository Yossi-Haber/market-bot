"""Evidence readiness is not publication approval or full discovery coverage."""
from .contracts import RESEARCH_SCHEMA, validate_packet
from .openai_provider import input_token_bound
from .research import INSTRUCTIONS
from urllib.parse import urlsplit


def assess_readiness(packet, coverage, *, max_input_tokens=50000, max_output_tokens=4000, model="gpt-4.1-mini", pricing=None, synthetic=False):
    result = {"market_date": packet.get("market_date"), "status": "INSUFFICIENT_SOURCE_EVIDENCE", "reasons": [],
              "model_candidate": model, "publication_approved": False, "coverage_complete": False}
    try:
        validate_packet(packet)
    except (ValueError, TypeError, KeyError):
        result["reasons"] = ["EVIDENCE_PACKET_OR_TIMESTAMPS_INVALID"]
        return result
    bound = input_token_bound(packet, INSTRUCTIONS, RESEARCH_SCHEMA)
    result.update(input_token_upper_estimate=bound, max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens,
                  estimated_maximum_cost_usd=str(pricing.cost(max_input_tokens, max_output_tokens)) if pricing else None,
                  estimated_packet_cost_usd=str(pricing.cost(bound, max_output_tokens)) if pricing else None)
    origin = packet["diagnostics"].get("origin", "")
    fake_hosts = {"example.com", "example.org", "example.net"}
    known_fixture = any((urlsplit(e["original_url"]).hostname or "").removeprefix("www.") in fake_hosts or (urlsplit(e["original_url"]).hostname or "").endswith((".example", ".invalid", ".test")) for e in packet["evidence"])
    if synthetic or known_fixture or "SYNTHETIC" in origin.upper() or not packet["diagnostics"].get("source_files"):
        result["reasons"] = ["GENUINE_SOURCE_PROVENANCE_NOT_ESTABLISHED"]
        return result
    if bound > max_input_tokens:
        result.update(status="BUDGET_EXCEEDED", reasons=["INPUT_TOKEN_UPPER_ESTIMATE_EXCEEDS_CAP"])
        return result
    usable = [e for e in packet["evidence"] if e["source_text"].strip() and e["content_completeness"] != "TITLE_ONLY"
              and e["temporal_tag"] not in {"TIME_UNCERTAIN", "POST_CLOSE"}]
    result["text_supported_movers"] = sorted({m["symbol"] for m in packet["movers"] if any(m["symbol"] in e["symbols"] for e in usable)})
    result["uncovered_movers"] = sorted({m["symbol"] for m in packet["movers"]} - set(result["text_supported_movers"]))
    if not usable or not result["text_supported_movers"]:
        if not any(c["state"] == "AVAILABLE" for c in packet["coverage"]) and any(c["state"] in {"UNAVAILABLE", "QUOTA_EXHAUSTED"} for c in packet["coverage"]):
            result["status"] = "PROVIDER_COVERAGE_GAP"
        result["reasons"] = ["NO_USABLE_PRE_CLOSE_SOURCE_TEXT_FOR_MOVERS"]
        return result
    context = packet["market_context"]
    gaps = []
    sector_data = context.get("sectors", {})
    sector_rows = sector_data.get("all", []) if isinstance(sector_data, dict) else sector_data
    if not context.get("indices") or not sector_rows:
        gaps.append("MARKET_OR_SECTOR_CONTEXT_MISSING")
    mover_symbols = {m["symbol"] for m in packet["movers"]}
    if not any(mover_symbols.intersection(members) for members in context.get("peer_membership", {}).values()):
        gaps.append("PEER_CONTEXT_MISSING")
    if not any(c["state"] == "AVAILABLE" for c in packet["coverage"]):
        gaps.append("NO_SUCCESSFUL_PROVIDER_COVERAGE")
    if packet["diagnostics"].get("capture", {}).get("observer_failed"):
        gaps.append("CAPTURE_OBSERVER_FAILED")
    if packet["diagnostics"].get("capture", {}).get("report_pipeline_completed") is False:
        gaps.append("REPORT_ONLY_COLLECTION_INTERRUPTED")
    if packet["diagnostics"].get("capture", {}).get("budget_excluded", 0):
        result.update(status="BUDGET_EXCEEDED", reasons=["CAPTURE_ITEM_BUDGET_EXCLUDED_EVIDENCE"])
        return result
    if gaps:
        result.update(status="PROVIDER_COVERAGE_GAP", reasons=gaps)
        return result
    result.update(status="READY_FOR_LLM_RESEARCH", reasons=["BOUNDED_PARTIAL_RESEARCH_ONLY_ABSTAIN_ON_UNCOVERED_MOVERS"],
                  limitations=packet["coverage"])
    return result
