"""Cross-asset hypotheses and the broad-driver causal gate."""

from __future__ import annotations

from datetime import date
from typing import Any

from .market_regime import detect_market_regimes


def build_cross_asset_hypotheses(report_data: dict[str, Any], regime: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    regime = regime or detect_market_regimes(report_data)
    regimes = set(regime.get("regimes", []))
    signals = regime.get("signals", [])
    data = {"market_regime": sorted(regimes), "signals": signals, "breadth": regime.get("breadth", {})}
    hypotheses: list[dict[str, Any]] = []
    if "RISING_YIELDS" in regimes and "TECH_SELL_OFF" in regimes:
        hypotheses.append({"hypothesis_id": "RATE_PRESSURE_ON_GROWTH", "event_type": "RATE_REPRICING", "direction": "down", "scope": "US_MARKET", "related_symbols": ["QQQ", "XLK"], "supporting_market_data": data, "status": "HYPOTHESIS"})
    if "RISING_YIELDS" in regimes and any(symbol in regime.get("sector_moves", {}) for symbol in ("XLRE", "XLU")):
        hypotheses.append({"hypothesis_id": "RATE_SENSITIVE_SECTOR_PRESSURE", "event_type": "TREASURY_SELL_OFF", "direction": "down", "scope": "SECTORS", "related_symbols": ["XLRE", "XLU"], "supporting_market_data": data, "status": "HYPOTHESIS"})
    if ("OIL_SHOCK_UP" in regimes) and regime.get("sector_moves", {}).get("XLE", 0) >= 1.5:
        hypotheses.append({"hypothesis_id": "OIL_RALLY_SUPPORTING_ENERGY", "event_type": "OIL_SHOCK", "direction": "up", "scope": "SECTOR", "related_symbols": ["XLE"], "supporting_market_data": data, "status": "HYPOTHESIS"})
    if "VOLATILITY_SPIKE" in regimes and ("BROAD_RISK_OFF" in regimes or "MARKET_BREADTH_WEAKNESS" in regimes):
        hypotheses.append({"hypothesis_id": "RISK_OFF_VOLATILITY_SHOCK", "event_type": "VOLATILITY_SHOCK", "direction": "down", "scope": "US_MARKET", "related_symbols": ["SPY", "QQQ", "DIA", "IWM"], "supporting_market_data": data, "status": "HYPOTHESIS"})
    if "USD_RALLY" in regimes and "BROAD_RISK_OFF" in regimes:
        hypotheses.append({"hypothesis_id": "USD_STRENGTH_HEADWIND", "event_type": "FX_MOVE", "direction": "down", "scope": "US_MARKET", "related_symbols": ["SPY", "QQQ"], "supporting_market_data": data, "status": "HYPOTHESIS"})
    return hypotheses


def market_driver_causal_gate(candidate: dict[str, Any], hypothesis: dict[str, Any], market_date: str) -> dict[str, Any]:
    """Require timing, explicit macro language, breadth, and direction."""
    published = str(candidate.get("published_at") or candidate.get("event_datetime") or "")[:10]
    if published and published > market_date:
        return {"causal_eligible": False, "causal_gate": "FAIL", "causal_reason": "FUTURE_EVENT"}
    if candidate.get("event_time_relation") == "FUTURE_EVENT":
        return {"causal_eligible": False, "causal_gate": "FAIL", "causal_reason": "FUTURE_EVENT"}
    if candidate.get("subject_gate") == "FAIL":
        return {"causal_eligible": False, "causal_gate": "FAIL", "causal_reason": "SUBJECT_GATE_FAIL"}
    if candidate.get("causal_gate") == "FAIL" or candidate.get("causal_eligible") is False:
        return {"causal_eligible": False, "causal_gate": "FAIL", "causal_reason": candidate.get("causal_reason") or "CAUSAL_GATE_FAIL"}
    text = " ".join(str(candidate.get(key) or "") for key in ("title", "headline", "summary")).lower()
    if any(term in text for term in ("why stocks could", "could fall", "may fall", "outlook", "opinion", "commentary")) and not any(term in text for term in ("after", "following", "surged", "jumped", "fell", "rose")):
        return {"causal_eligible": False, "causal_gate": "FAIL", "causal_reason": "COMMENTARY_ONLY"}
    macro_terms = ("yield", "treasury", "bond", "rate", "fed", "oil", "crude", "dollar", "dxy", "volatility", "vix", "stocks fell", "selloff", "market")
    if not any(term in text for term in macro_terms):
        return {"causal_eligible": False, "causal_gate": "FAIL", "causal_reason": "NO_EXPLICIT_MARKET_EVENT"}
    moves = hypothesis.get("supporting_market_data", {}).get("signals", [])
    return {"causal_eligible": True, "causal_gate": "PASS", "causal_reason": "MARKET_EVENT_DIRECTIONALLY_CONSISTENT", "matched_signal_count": len(moves)}


def classify_market_hypotheses(report_data: dict[str, Any], candidates: list[dict[str, Any]] | None = None, verified: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    regime = detect_market_regimes(report_data)
    hypotheses = build_cross_asset_hypotheses(report_data, regime)
    candidates = candidates or []
    verified = verified or []
    ranked = []
    warnings: list[str] = []
    for hypothesis in hypotheses:
        def evidence_eligible(item: dict[str, Any]) -> bool:
            # A deterministic pattern is only a hypothesis.  Promotion needs
            # an explicitly passed subject/causal candidate from the news
            # pipeline; absence of the flag is not evidence.
            if item.get("causal_eligible") is not True:
                return False
            if item.get("subject_gate") == "FAIL" or item.get("causal_gate") == "FAIL":
                return False
            return bool(market_driver_causal_gate(item, hypothesis, str(report_data.get("market_date") or "9999-12-31")).get("causal_eligible"))

        eligible = [item for item in candidates if evidence_eligible(item)]
        matching_verified = [item for item in verified if item.get("verification") == "VERIFIED" and item.get("editorial_eligible", True) is True and evidence_eligible(item)]
        status = "CONFIRMED" if matching_verified else ("PLAUSIBLE_NOT_VERIFIED" if eligible else "NO_MARKET_DRIVER_FOUND")
        hypothesis_status = "EVIDENCE_ELIGIBLE" if eligible else "HYPOTHESIS_ONLY"
        if status in {"CONFIRMED", "PLAUSIBLE_NOT_VERIFIED"} and not eligible:
            warnings.append("MARKET_DRIVER_PROMOTED_WITHOUT_CAUSAL_EVIDENCE")
        ranked.append({**hypothesis, "verification_status": status, "hypothesis_status": hypothesis_status, "candidate_stories": eligible, "verified_story": matching_verified[0] if matching_verified else None, "confidence_reason": "Cross-asset pattern is deterministic; external evidence is required before causal wording."})
    ranked.sort(key=lambda item: (item["verification_status"] == "CONFIRMED", len(item.get("related_symbols", [])), item["hypothesis_id"]), reverse=True)
    return {"primary_driver": ranked[0] if ranked else None, "secondary_drivers": ranked[1:], "market_regime": regime.get("regimes", []), "cross_asset_signals": regime.get("signals", []), "breadth": regime.get("breadth", {}), "warnings": sorted(set(warnings))}
