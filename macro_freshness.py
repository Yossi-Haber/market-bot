"""Whether a macro observation may support a same-session movement claim."""

from __future__ import annotations

from typing import Any


def current_macro(item: Any, market_date: str | None = None) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("freshness") in {"lagging", "stale"} or item.get("warning") in {"SOURCE_DATE_BEHIND_MARKET_DATE", "STALE_SOURCE_DATA"}:
        return False
    observed = item.get("latest_observation_date") or item.get("selected_source_date") or item.get("latest_bar_datetime")
    return not (market_date and observed and str(observed)[:10] != market_date)
