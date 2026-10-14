"""Provider-independent discovery adapters."""

from .base import NewsProvider, ProviderArticle
from .finnhub import FinnhubProvider
from .marketaux import MarketauxProvider
from .fmp import FMPProvider
from .strategy import CompanyNewsStrategy

__all__ = ["NewsProvider", "ProviderArticle", "FinnhubProvider", "MarketauxProvider", "FMPProvider", "CompanyNewsStrategy"]
