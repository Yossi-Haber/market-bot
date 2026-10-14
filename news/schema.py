"""Shared normalized news-candidate schema."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class NewsCandidate:
    """Normalized internal candidate shared by official and discovery providers."""

    title: str
    summary: str | None
    published_at: str | None
    source_name: str
    source_domain: str
    source_tier: int
    source_url: str
    category: str
    affected_symbols: list[str] = field(default_factory=list)
    discovery_source: str = "official"
    official: bool = False
    verification: str = "NOT_CONFIRMED"
    content_type: str = "OTHER"
    editorial_eligible: bool = False
    article_type: str = "OTHER"
    discovery_score: float = 0.0
    entity_names: list[str] = field(default_factory=list)
    provider_article_id: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
