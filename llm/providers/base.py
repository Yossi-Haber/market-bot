"""Provider-neutral interface for narrative generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProviderError(RuntimeError):
    """Safe wrapper for provider failures."""


class LLMProvider(ABC):
    """Abstract provider receiving only an already-filtered payload."""

    @abstractmethod
    def generate_narrative(self, payload: dict[str, Any]) -> str:
        """Generate plain Markdown narrative text."""
        raise NotImplementedError
