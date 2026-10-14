"""Provider-interface adapter around the existing Marketaux client."""

from __future__ import annotations

from news.marketaux import MarketauxDiscovery
from news.schema import NewsCandidate


class MarketauxProvider:
    name = "marketaux"

    def __init__(self, client: MarketauxDiscovery) -> None:
        self.client = client

    def search_company(self, symbol: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        return self._normalize(self.client.search("", "TARGETED_MOVER", [symbol]))

    def search_general(self, query: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        return self._normalize(self.client.search(query, "GENERAL"))

    def search_topic(self, topic: str, start_date: str, end_date: str) -> list[NewsCandidate]:
        return self._normalize(self.client.search(topic, "TOPIC"))

    @staticmethod
    def _normalize(items: list[NewsCandidate]) -> list[NewsCandidate]:
        for item in items:
            item.provider_article_id = item.source_url or item.title
        return items

    @property
    def available(self) -> bool:
        return self.client.enabled and self.client.available_for_run
