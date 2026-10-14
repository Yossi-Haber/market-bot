"""Central market instrument configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os


MOVERS_UNIVERSE_LIMIT = 120
MOVERS_MAX_DAILY_CREDITS = 500
MOVERS_BATCH_SIZE = 8
TOP_GAINERS_COUNT = 5
TOP_LOSERS_COUNT = 5
MACRO_MAX_LAG_DAYS = 3
DISCOVERY_MAX_QUERIES_PER_SYMBOL = 3
DISCOVERY_MAX_RESULTS_PER_QUERY = 10
DISCOVERY_REQUEST_DELAY_SECONDS = 0.15
ALPHA_VANTAGE_MAX_DAILY_REQUESTS = 10
ENABLE_ALPHA_VANTAGE = False
TARGETED_SEARCH_MAX_QUERIES_PER_SYMBOL = 4
TARGETED_SEARCH_MAX_REQUESTS_PER_RUN = 40
ENABLE_LLM = False
LLM_PROVIDER = "openai"
LLM_MODEL = "gpt-5.6-luna"
LLM_TIMEOUT_SECONDS = 30


def load_llm_settings() -> dict[str, str | bool | int]:
    """Read optional LLM settings at runtime, preserving a disabled default."""
    enabled = os.getenv("ENABLE_LLM", "false").strip().lower() in {"1", "true", "yes", "on"}
    try:
        timeout = max(1, int(os.getenv("LLM_TIMEOUT_SECONDS", str(LLM_TIMEOUT_SECONDS))))
    except ValueError:
        timeout = LLM_TIMEOUT_SECONDS
    return {
        "enable_llm": enabled,
        "provider": os.getenv("LLM_PROVIDER", LLM_PROVIDER).strip().lower() or LLM_PROVIDER,
        "model": os.getenv("LLM_MODEL", LLM_MODEL).strip() or LLM_MODEL,
        "timeout_seconds": timeout,
    }


@dataclass(frozen=True)
class Instrument:
    """A logical instrument and its Twelve Data symbol."""

    symbol: str
    display_name: str
    category: str
    provider: str | None = "Twelve Data"
    provider_symbol: str | None = None
    fallback_provider: str | None = None
    fallback_symbol: str | None = None


MARKET_SYMBOLS: dict[str, dict[str, Instrument]] = {
    "core": {
        "SPY": Instrument("SPY", "SPDR S&P 500 ETF", "core"),
        "QQQ": Instrument("QQQ", "Invesco QQQ Trust", "core"),
        "DIA": Instrument("DIA", "SPDR Dow Jones Industrial Average ETF", "core"),
        "IWM": Instrument("IWM", "iShares Russell 2000 ETF", "core"),
    },
    "sectors": {
        "XLK": Instrument("XLK", "Technology Select Sector SPDR Fund", "sectors"),
        "XLF": Instrument("XLF", "Financial Select Sector SPDR Fund", "sectors"),
        "XLE": Instrument("XLE", "Energy Select Sector SPDR Fund", "sectors"),
        "XLV": Instrument("XLV", "Health Care Select Sector SPDR Fund", "sectors"),
        "XLY": Instrument("XLY", "Consumer Discretionary Select Sector SPDR Fund", "sectors"),
        "XLP": Instrument("XLP", "Consumer Staples Select Sector SPDR Fund", "sectors"),
        "XLI": Instrument("XLI", "Industrial Select Sector SPDR Fund", "sectors"),
        "XLB": Instrument("XLB", "Materials Select Sector SPDR Fund", "sectors"),
        "XLRE": Instrument("XLRE", "Real Estate Select Sector SPDR Fund", "sectors"),
        "XLU": Instrument("XLU", "Utilities Select Sector SPDR Fund", "sectors"),
        "XLC": Instrument("XLC", "Communication Services Select Sector SPDR Fund", "sectors"),
    },
    "macro": {
        "VIX": Instrument("VIX", "CBOE Volatility Index", "macro", "CBOE", "VIX", "FRED", "VIXCLS"),
        "GOLD": Instrument("XAU/USD", "Gold Spot / US Dollar", "macro"),
        "WTI": Instrument("WTI", "WTI Crude Oil", "macro", "FRED", "DCOILWTICO"),
        "DXY": Instrument("DXY", "US Dollar Index", "macro", "Yahoo Finance", "DX-Y.NYB"),
        "US10Y": Instrument("US10Y", "US 10-Year Treasury Yield", "macro", "FRED", "DGS10"),
    },
}


def all_instruments() -> list[Instrument]:
    """Return every configured instrument once, in stable category order."""
    return [instrument for group in MARKET_SYMBOLS.values() for instrument in group.values()]
