"""Typed schemas for the provider-neutral LLM safety boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GuardrailResult:
    """Deterministic validation result for hypothetical narrative text."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}
