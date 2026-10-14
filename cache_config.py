"""Central cache-root resolution for production and isolated tests."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def cache_root() -> Path:
    """Return the configured cache root, defaulting to the project cache."""
    configured = os.getenv("MARKET_BOT_CACHE_DIR", "").strip()
    return Path(configured).expanduser() if configured else PROJECT_ROOT / "cache"


def cache_path(*parts: str) -> Path:
    """Build a path below the configured cache root."""
    return cache_root().joinpath(*parts)
