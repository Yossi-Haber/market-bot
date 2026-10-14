"""Validation helpers for collected market data."""

from __future__ import annotations

from math import isfinite
from numbers import Real
from typing import Any


def validate_market_result(result: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate one market result and return ``(is_valid, messages)``."""
    errors: list[str] = []
    for field in ("close", "previous_close"):
        value = result.get(field)
        if not isinstance(value, Real) or isinstance(value, bool) or value <= 0:
            errors.append(f"{field} must be numeric and greater than zero")

    change_pct = result.get("change_pct")
    if not isinstance(change_pct, Real) or isinstance(change_pct, bool) or not isfinite(float(change_pct)):
        errors.append("change_pct must be finite")

    if not result.get("latest_bar_datetime"):
        errors.append("latest_bar_datetime is missing")

    high, low = result.get("high"), result.get("low")
    if high is not None and low is not None:
        if not _positive_number(high) or not _positive_number(low) or high < low:
            errors.append("high and low must be positive with high >= low")
        close = result.get("close")
        if _positive_number(high) and _positive_number(low) and _positive_number(close):
            if not (low <= close <= high):
                errors.append("close must be within the daily high/low range")

    open_value = result.get("open")
    if open_value is not None and not _positive_number(open_value):
        errors.append("open must be a positive numeric value when present")

    volume = result.get("volume")
    if volume is not None and (not isinstance(volume, Real) or isinstance(volume, bool) or volume < 0):
        errors.append("volume must be numeric and non-negative when present")

    if errors:
        result["verified"] = False
        result["validation_errors"] = errors
        return False, errors

    result["verified"] = bool(result.get("verified", True))
    if abs(float(change_pct)) > 20:
        result["warning"] = "SUSPICIOUS_DAILY_MOVE"
    return bool(result["verified"]), []


def validate_fred_result(result: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate normalized FRED observations without equity OHLC rules."""
    errors: list[str] = []
    for field in ("close", "previous_close"):
        value = result.get(field)
        if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(float(value)):
            errors.append(f"{field} must be finite numeric data")
    for field in ("latest_observation_date", "previous_observation_date"):
        try:
            from datetime import date
            date.fromisoformat(str(result.get(field)))
        except (TypeError, ValueError):
            errors.append(f"{field} must be a valid ISO date")
    if result.get("symbol") == "US10Y":
        bps = result.get("change_bps")
        if not isinstance(bps, Real) or isinstance(bps, bool) or not isfinite(float(bps)):
            errors.append("change_bps must be finite numeric data")
    change = result.get("change")
    if not isinstance(change, Real) or isinstance(change, bool) or not isfinite(float(change)):
        errors.append("change must be finite numeric data")
    if errors:
        result["verified"] = False
        result["validation_errors"] = errors
        return False, errors
    return bool(result.get("verified")), []


def validate_macro_ohlc_result(result: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate VIX/DXY normalized OHLC data without requiring volume."""
    errors: list[str] = []
    for field in ("close", "previous_close"):
        if not _positive_number(result.get(field)):
            errors.append(f"{field} must be numeric and greater than zero")
    change_pct = result.get("change_pct")
    if not isinstance(change_pct, Real) or isinstance(change_pct, bool) or not isfinite(float(change_pct)):
        errors.append("change_pct must be finite")
    try:
        from datetime import date
        latest_date = date.fromisoformat(str(result.get("latest_observation_date")))
        previous_date = date.fromisoformat(str(result.get("previous_observation_date")))
        if latest_date < previous_date:
            errors.append("observation dates are not ordered")
    except (TypeError, ValueError):
        errors.append("observation dates must be valid ISO dates")
    high, low, close = result.get("high"), result.get("low"), result.get("close")
    if high is not None and low is not None:
        if not _positive_number(high) or not _positive_number(low) or high < low:
            errors.append("high and low must be positive with high >= low")
        elif not (low <= close <= high):
            errors.append("close must be within the daily high/low range")
    open_value = result.get("open")
    if open_value is not None and not _positive_number(open_value):
        errors.append("open must be a positive numeric value when present")
    if errors:
        result["verified"] = False
        result["validation_errors"] = errors
        return False, errors
    return bool(result.get("verified")), []


def _positive_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and isfinite(float(value)) and value > 0
