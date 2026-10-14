"""Standalone entry point for the evidence-first catalyst engine."""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

from app import configure_logging
from news.catalyst import run_catalyst_snapshot


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    run_catalyst_snapshot(configure_logging())
