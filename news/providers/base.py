"""Shared secondary-news provider contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from news.schema import NewsCandidate


@dataclass
class ProviderArticle:
    """Normalized provider response before verification."""

    provider: str
    provider_article_id: str
    headline: str
    summary: str | None
    url: str
    source: str
    published_at: str | None
    symbols: list[str] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_news_candidate(self) -> NewsCandidate:
        return NewsCandidate(self.headline, self.summary, self.published_at, self.source, "", 3, self.url, self.topics[0] if self.topics else "OTHER", self.symbols, self.provider, False, "NOT_CONFIRMED", "OTHER", False, "NEWS_EVENT", 0.0, [str(item.get("name")) for item in self.entities if item.get("name")])


class NewsProvider(Protocol):
    """Minimal provider interface used by company/general/topic strategy."""

    name: str

    def search_company(self, symbol: str, start_date: str, end_date: str) -> list[NewsCandidate]: ...
    def search_general(self, query: str, start_date: str, end_date: str) -> list[NewsCandidate]: ...
    def search_topic(self, topic: str, start_date: str, end_date: str) -> list[NewsCandidate]: ...
