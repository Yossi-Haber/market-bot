"""Explicit A3 execution modes; never imported by production orchestration."""
from __future__ import annotations

import argparse
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from .artifacts import assess_coverage, load_archive
from .contracts import RESEARCH_SCHEMA, validate_packet
from .openai_provider import OpenAIResearchProvider, Pricing, input_token_bound
from .research import FakeProvider, INSTRUCTIONS, ResearchRunner
from .validation import validate_ledger
from .readiness import assess_readiness

MODES = ("offline", "real-evidence", "real-llm")


def public_json(value):
    """Reject credential-shaped fields before exporting acquisition or model data."""
    import re
    def inspect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in {"api_key", "apikey", "api_token", "access_token", "authorization", "secret", "password", "telegram_chat_ids", "telegram_bot_token"}:
                    raise ValueError("ARTIFACT_CREDENTIAL_FIELD_BLOCKED")
                inspect(child)
        elif isinstance(item, list):
            for child in item: inspect(child)
    inspect(value)
    text = json.dumps(value, ensure_ascii=False, indent=2)
    key = os.getenv("OPENAI_API_KEY")
    patterns = (r"sk-[A-Za-z0-9_-]{20,}", r"gh[pousr]_[A-Za-z0-9]{20,}", r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b")
    if (key and key in text) or any(re.search(p, text) for p in patterns):
        raise ValueError("ARTIFACT_SECRET_LEAK_BLOCKED")
    return text


def review_markdown(packet, coverage, raw, ledger, usage, mode):
    def block(value):
        return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"
    lines = [f"# V2 English research review — {packet['market_date']}", f"Mode: {mode}. Origin: {packet['diagnostics'].get('origin')}. Research status: {raw['status']}.",
             "Research assessment only; source quotations and model proposals are not publication approval.",
             "## Major movers", block(packet["movers"]), "## Evidence coverage and missing bodies", block(coverage),
             "## Proposed events and explanations", block(raw.get("research", {"status": "NOT_ASSESSED"})),
             "## Approved events and source facts", block({k: ledger.get(k, []) for k in ("approved_events", "approved_claims")}),
             "## Approved explanations and unexplained moves", block(ledger.get("movement_assessments", {"status": "NOT_ASSESSED"})),
             "## Rejected, downgraded and review-required claims", block(ledger.get("rejected_claims", [])),
             "## Source citations and exact quotations", block(ledger.get("citation_audit", [])),
             "## Sector observations", block(packet["market_context"].get("sectors", packet["peer_groups"])),
             "## Human review requirements", "Inspect source authenticity, missing content, ambiguous semantics, alternative explanations and every downgrade. "
             "SEMANTIC_REVIEW_REQUIRED claims remain unapproved. Missing archived evidence is an acquisition gap, not an LLM reasoning failure.",
             "## Usage and cost estimates", block(usage)]
    return "\n\n".join(lines) + "\n"


def generate_package(*, mode, output, market_date=None, archive=None, recordings=None, packet_path=None,
                     response_path=None, source_policy=None, model=None, pricing=None, authorized=False,
                     max_spend_usd=None, max_calls=1, max_input_tokens=50000, max_output_tokens=4000, timeout_seconds=60):
    if mode not in MODES or (authorized and mode != "real-llm"):
        raise ValueError("EXECUTION_MODE_INVALID")
    root = Path(output)
    if root.exists() and any(root.iterdir()):
        raise ValueError("OUTPUT_MUST_BE_NEW_EMPTY_DIRECTORY")
    def blocked(*args, **kwargs):
        raise RuntimeError("V2_OFFLINE_NETWORK_BLOCKED")
    with ExitStack() as stack:
        if mode != "real-llm":
            for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo", "socket.create_connection"):
                stack.enter_context(patch(target, blocked))
        if mode == "offline":
            if not packet_path or not response_path or archive:
                raise ValueError("OFFLINE_FIXTURES_REQUIRED")
            packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
        else:
            if not archive or not market_date or packet_path or response_path:
                raise ValueError("GENUINE_ARCHIVE_REQUIRED")
            packet = load_archive(archive, market_date, recordings)
        validate_packet(packet)
        coverage = assess_coverage(packet)
        readiness = assess_readiness(packet, coverage, max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens,
                                     model=model or "gpt-4.1-mini", pricing=pricing, synthetic=mode == "offline")
        root.mkdir(parents=True, exist_ok=True)
        # Freeze acquisition/coverage before any possible paid request.
        for name, value in (("v2_evidence_packet.json", packet), ("v2_evidence_coverage.json", coverage), ("v2_research_readiness.json", readiness)):
            (root/name).write_text(public_json(value), encoding="utf-8")
        policy = json.loads(Path(source_policy).read_text(encoding="utf-8")) if source_policy else {}
        usage = {"api_calls": 0, "simulated": mode == "offline", "status": "NOT_CALLED"}
        if pricing:
            if not model or pricing.model != model:
                raise ValueError("PRICING_MODEL_MISMATCH")
            bound = input_token_bound(packet, INSTRUCTIONS, RESEARCH_SCHEMA)
            usage.update(model_requested=model, input_token_upper_estimate=bound, max_output_tokens=max_output_tokens,
                         estimated_single_call_upper_cost_usd=str(pricing.cost(bound, max_output_tokens)), pricing=pricing.__dict__)
        raw = {"status": "NOT_REQUESTED", "reason": "REAL_EVIDENCE_NO_LLM"}
        provider = None
        if mode == "offline":
            fake = FakeProvider(json.loads(Path(response_path).read_text(encoding="utf-8")))
            raw = ResearchRunner(fake, model="synthetic-replay", max_total_tokens=100000).run(packet)
            usage.update(raw.get("diagnostics", {}))
            usage["status"] = raw["status"]
        elif mode == "real-llm":
            reasons = []
            if not authorized: reasons.append("PAID_LLM_AUTHORIZATION_REQUIRED")
            if not os.getenv("OPENAI_API_KEY"): reasons.append("LLM_CREDENTIALS_UNAVAILABLE")
            if not coverage["reasoning_testable"]: reasons.append("EVIDENCE_INPUTS_INCOMPLETE")
            if readiness["status"] != "READY_FOR_LLM_RESEARCH": reasons.append(readiness["status"])
            if not model or not pricing or max_spend_usd is None: reasons.append("MODEL_PRICING_SPENDING_CONFIG_REQUIRED")
            if reasons:
                raw = {"status": "BLOCKED", "reasons": reasons}
            else:
                provider = OpenAIResearchProvider(api_key=os.environ["OPENAI_API_KEY"], model=model, pricing=pricing,
                    max_spend_usd=max_spend_usd, authorized=authorized, max_calls=max_calls, max_input_tokens=max_input_tokens,
                    max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds)
                raw = ResearchRunner(provider, model=model, credential_available=True, max_requests=max_calls,
                    max_input_bytes=max_input_tokens, max_total_tokens=max_input_tokens+max_output_tokens,
                    max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds+1).run(packet)
                # Preserve model text for audit even if local schema validation rejects it.
                raw["raw_output_text"] = provider.last_text
                usage.update(provider.usage)
                if raw["status"] == "TIMEOUT":
                    usage.update(status="UNKNOWN_OUTCOME_TIMEOUT", outcome_cost_uncertain=True)
        ledger = validate_ledger(packet, raw["research"], source_policy=policy) if raw["status"] == "PROPOSAL" else {
            "ledger_version": 2, "status": "NOT_ASSESSED", "reason": raw["status"], "audit_trail": []}
        outputs = {"v2_raw_research_ledger.json": raw, "v2_validated_research_ledger.json": ledger,
                   "v2_validation_audit.json": {"claims": ledger.get("audit_trail", []), "citations": ledger.get("citation_audit", [])},
                   "v2_llm_usage.json": usage}
        rendered = {name: public_json(value) for name, value in outputs.items()}
        rendered["v2_research_review.md"] = review_markdown(packet, coverage, raw, ledger, usage, mode)
        key = os.getenv("OPENAI_API_KEY") if mode == "real-llm" else None
        if key and any(key in value for value in rendered.values()):
            raise ValueError("ARTIFACT_SECRET_LEAK_BLOCKED")
        for name, value in rendered.items():
            (root/name).write_text(value, encoding="utf-8")
        return {"status": raw["status"], "coverage": coverage, "usage": usage, "readiness": readiness}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, required=True)
    for flag in ("output", "market-date", "archive", "recordings", "packet", "response", "source-policy", "pricing"):
        parser.add_argument("--"+flag, required=flag == "output")
    parser.add_argument("--model", default=os.getenv("MARKETBOT_V2_RESEARCH_MODEL"))
    parser.add_argument("--authorize-paid-llm", action="store_true")
    parser.add_argument("--max-spend-usd")
    parser.add_argument("--max-calls", type=int, default=1)
    parser.add_argument("--max-input-tokens", type=int, default=50000)
    parser.add_argument("--max-output-tokens", type=int, default=4000)
    parser.add_argument("--timeout-seconds", type=float, default=60)
    args = parser.parse_args()
    try:
        pricing = Pricing(**json.loads(Path(args.pricing).read_text(encoding="utf-8"))) if args.pricing else None
        result = generate_package(mode=args.mode, output=args.output, market_date=args.market_date, archive=args.archive,
            recordings=args.recordings, packet_path=args.packet, response_path=args.response, source_policy=args.source_policy,
            model=args.model, pricing=pricing, authorized=args.authorize_paid_llm, max_spend_usd=args.max_spend_usd,
            max_calls=args.max_calls, max_input_tokens=args.max_input_tokens, max_output_tokens=args.max_output_tokens, timeout_seconds=args.timeout_seconds)
        print("V2_SESSION="+result["status"])
        print("USABLE_EVIDENCE_TEXT_ITEMS="+str(result["coverage"]["usable_text_items"]))
        print("LLM_API_CALLS="+str(result["usage"]["api_calls"]))
        print("RESEARCH_READINESS="+result["readiness"]["status"])
        return 0 if result["status"] in {"PROPOSAL", "NOT_REQUESTED"} else 1
    except Exception as exc:
        print("V2_SESSION_FAILED="+(str(exc) if isinstance(exc, ValueError) and str(exc).isupper() else type(exc).__name__))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
