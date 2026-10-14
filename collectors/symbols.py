"""Provider symbol normalization."""

from __future__ import annotations


TWELVE_DATA_SYMBOL_MAP = {
    # Twelve Data accepts the dot form for these U.S. share classes.
    "BRK.B": "BRK.B",
    "BF.B": "BF.B",
}


def normalize_symbol_for_twelve_data(symbol: str) -> str:
    """Return the Twelve Data symbol for a canonical universe symbol."""
    return TWELVE_DATA_SYMBOL_MAP.get(symbol, symbol)
