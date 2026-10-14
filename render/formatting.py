"""Small, deterministic formatting helpers used by the report renderer."""

from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> float:
    """Return a finite float or zero for a missing/non-numeric value."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def format_pct(value: Any) -> str:
    """Format a percentage with an explicit sign and two decimals."""
    number = _number(value)
    if abs(number) < 0.005:
        number = 0.0
    return f"{number:+.2f}%"


def format_price(value: Any, *, thousands_separator: bool = False) -> str:
    """Format a market value to two decimals, optionally with grouping commas."""
    return f"{_number(value):,.2f}" if thousands_separator else f"{_number(value):.2f}"


def format_bps(value: Any) -> str:
    """Format basis points as an integer when possible, otherwise one decimal."""
    number = _number(value)
    if number.is_integer():
        return f"{number:+.0f} נ\"ב"
    return f"{number:+.1f} נ\"ב"


def format_signed_number(value: Any) -> str:
    """Format a signed numeric value with two decimals."""
    return f"{_number(value):+.2f}"
