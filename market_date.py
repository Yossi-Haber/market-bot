"""Canonical US-equity session date resolution."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any


class MarketDateResolutionError(RuntimeError):
    """Raised when core equity evidence cannot establish one session date."""


@dataclass(frozen=True)
class MarketDateResolution:
    """Resolved date and safe audit metadata."""

    market_date: str
    source: str
    agreement: str
    warning: str | None = None


def _iso_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def resolve_market_date(core_results: dict[str, dict[str, Any]], explicit_date: str | None = None) -> MarketDateResolution:
    """Resolve from an explicit date or a 3-of-4 core ETF consensus.

    Macro, FX, commodity, VIX, and calendar dates are intentionally not inputs.
    """
    if explicit_date:
        value = _iso_date(explicit_date)
        if value != explicit_date:
            raise MarketDateResolutionError("INVALID_EXPLICIT_MARKET_DATE")
        return MarketDateResolution(value, "EXPLICIT", "explicit")

    dates = {
        symbol: _iso_date(item.get("latest_bar_datetime"))
        for symbol, item in core_results.items()
        if isinstance(item, dict) and item.get("verified") is True
    }
    valid = [value for value in dates.values() if value]
    if len(valid) < 3:
        raise MarketDateResolutionError("CORE_ETF_AGREEMENT_BELOW_THRESHOLD")
    counts = Counter(valid)
    market_date, agreement_count = counts.most_common(1)[0]
    if agreement_count < 3:
        raise MarketDateResolutionError("CORE_ETF_AGREEMENT_BELOW_THRESHOLD")
    agreement = f"{agreement_count}/4"
    warning = "CORE_ETF_CONSENSUS_3_OF_4" if agreement_count == 3 else None
    return MarketDateResolution(market_date, "CORE_ETF_CONSENSUS", agreement, warning)


def select_as_of_bars(item: dict[str, Any], canonical_date: str, *, require_exact: bool = False) -> dict[str, Any]:
    """Select two completed bars on or before a date.

    Production callers retain the historical as-of behavior by default.  A
    historical price snapshot can request ``require_exact`` so a missing
    observation on the requested session is rejected instead of silently
    substituted with an older bar.
    """
    bars = [bar for bar in item.get("completed_bars", []) if isinstance(bar, dict) and _iso_date(bar.get("datetime")) and _iso_date(bar.get("datetime")) <= canonical_date]
    bars.sort(key=lambda bar: str(bar.get("datetime")), reverse=True)
    if require_exact and (not bars or _iso_date(bars[0].get("datetime")) != canonical_date):
        result = dict(item)
        result["verified"] = False
        result["error"] = "MISSING_EXACT_HISTORICAL_DATE"
        result["selected_source_date"] = None
        return result
    if len(bars) < 2:
        return item
    latest, previous = bars[0], bars[1]
    close, previous_close = float(latest["close"]), float(previous["close"])
    result = dict(item)
    result.update({"close": round(close, 6), "previous_close": round(previous_close, 6), "change": round(close - previous_close, 6), "change_pct": round(((close / previous_close) - 1) * 100, 6), "latest_bar_datetime": str(latest["datetime"]), "selected_source_date": _iso_date(latest["datetime"]), "previous_source_date": _iso_date(previous["datetime"])})
    return result
