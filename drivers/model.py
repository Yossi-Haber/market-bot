"""Typed, provider-independent market-driver records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DriverType(str, Enum):
    MARKET_DRIVER = "MARKET_DRIVER"
    SECTOR_DRIVER = "SECTOR_DRIVER"
    STOCK_CATALYST = "STOCK_CATALYST"


class DriverStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    PLAUSIBLE_NOT_VERIFIED = "PLAUSIBLE_NOT_VERIFIED"
    NO_CATALYST_FOUND = "NO_CATALYST_FOUND"


class ExplanationStatus(str, Enum):
    """Overall explanation level, distinct from driver verification state."""

    STOCK_SPECIFIC = "STOCK_SPECIFIC"
    SECTOR_EXPLAINED = "SECTOR_EXPLAINED"
    MARKET_EXPLAINED = "MARKET_EXPLAINED"
    MULTI_FACTOR = "MULTI_FACTOR"
    UNEXPLAINED = "UNEXPLAINED"


@dataclass
class Driver:
    """A deterministic hypothesis plus separate evidence/verification state."""

    driver_id: str
    driver_type: str
    scope: str
    direction: str
    strength: float
    related_symbols: list[str] = field(default_factory=list)
    related_sector: str | None = None
    movement_evidence: list[dict[str, Any]] = field(default_factory=list)
    candidate_stories: list[dict[str, Any]] = field(default_factory=list)
    verified_story: dict[str, Any] | None = None
    verification_status: str = DriverStatus.PLAUSIBLE_NOT_VERIFIED.value
    confidence_reason: str = "Movement evidence is a hypothesis; no independent verification yet."

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
