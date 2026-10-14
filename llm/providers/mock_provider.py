"""Offline provider used to exercise the narrative safety pipeline."""

from __future__ import annotations

from typing import Any

from .base import LLMProvider, ProviderError


class MockProvider(LLMProvider):
    """Return deterministic valid/invalid narratives or simulate provider failures."""

    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode
        self.calls = 0

    def generate_narrative(self, payload: dict[str, Any]) -> str:
        self.calls += 1
        if self.mode == "timeout":
            raise TimeoutError("mock timeout")
        if self.mode == "empty":
            return ""
        if self.mode == "numeric":
            return "SPY עלה ב-4.54%."
        if self.mode == "causal":
            return "PYPL עלתה בעקבות רכישה."
        if self.mode == "ticker":
            return "XYZ זינקה ב-5%."
        if self.mode == "lagging":
            return "WTI נסגר היום על 96.41."
        return "# סיכום השוק היומי\n\nSPY עלה ב-0.54% ו-DIA עלה ב-0.94%."
