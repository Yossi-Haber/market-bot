"""Safe narrative pipeline: filtered input -> provider -> guardrails -> fallback."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from config import load_llm_settings
from render.markdown import ReportNotReadyError, load_report_data, render_report
from .guardrails import choose_publishable_report, validate_llm_output
from .input_builder import LLMInputNotReadyError, build_llm_payload
from .providers import LLMProvider, OpenAIProvider, ProviderError

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
DEBUG_DIR = OUTPUT_DIR / "debug"


def select_provider(settings: dict[str, Any], logger: logging.Logger) -> LLMProvider | None:
    """Construct the configured provider, returning None when unavailable."""
    if settings["provider"] != "openai":
        logger.error("LLM_PROVIDER_UNAVAILABLE")
        return None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.error("LLM_PROVIDER_UNAVAILABLE")
        return None
    return OpenAIProvider(api_key=api_key, model=str(settings["model"]), timeout_seconds=int(settings["timeout_seconds"]))


def load_deterministic_markdown(report_data: dict[str, Any]) -> str:
    """Use the existing deterministic report, rendering in memory if needed."""
    path = OUTPUT_DIR / "daily_report.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return render_report(report_data)


def write_selection(selection: dict[str, Any]) -> Path:
    """Persist selection metadata without narrative secrets or API keys."""
    path = OUTPUT_DIR / "daily_report_selection.json"
    path.write_text(json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _public_transparency_text(markdown: str, report_data: dict[str, Any]) -> str:
    """Append the deterministic unexplained-move notice shared with the poster."""
    notices = [item for item in report_data.get("unexplained_move_notices", []) if isinstance(item, dict) and item.get("status") == "UNEXPLAINED" and item.get("message_he") and str(item["message_he"]) not in markdown]
    if not notices:
        return markdown
    lines = ["## תנועות משמעותיות ללא הסבר מאומת", ""]
    if not report_data.get("verified_stories"):
        lines.extend(["לא קודמו היום סיפורים עם גורם מאומת.", ""])
    lines.extend(str(item["message_he"]) for item in notices)
    return markdown.rstrip() + "\n\n" + "\n\n".join(lines) + "\n"


def _write_final(markdown: str, report_data: dict[str, Any]) -> Path:
    path = OUTPUT_DIR / "daily_report_final.md"
    path.write_text(_public_transparency_text(markdown, report_data), encoding="utf-8")
    return path


def run_narrative(provider: LLMProvider | None = None, logger: logging.Logger | None = None) -> Path | None:
    """Run one optional generation attempt and always select a safe output."""
    load_dotenv(BASE_DIR / ".env")
    logger = logger or logging.getLogger("market_bot.llm")
    started = time.monotonic()
    report_path = OUTPUT_DIR / "daily_report_data.json"
    try:
        report_data = load_report_data(report_path)
        payload = build_llm_payload(report_data)
    except (ReportNotReadyError, LLMInputNotReadyError):
        logger.error("REPORT_NOT_READY")
        write_selection({"selected_source": "NONE", "guardrails_valid": False, "errors": ["REPORT_NOT_READY"], "warnings": [], "fallback_used": False})
        return None

    deterministic = load_deterministic_markdown(report_data)
    settings = load_llm_settings()
    logger.info("LLM model configured: %s", settings["model"])
    if not settings["enable_llm"]:
        logger.info("LLM disabled; using deterministic report")
        _write_final(deterministic, report_data)
        write_selection({"selected_source": "DETERMINISTIC", "selection_reason": "LLM_DISABLED", "llm_attempted": False, "guardrails_checked": False, "guardrails_valid": False, "errors": [], "warnings": [], "fallback_used": True})
        return OUTPUT_DIR / "daily_report_final.md"

    provider = provider or select_provider(settings, logger)
    if provider is None:
        logger.error("Using deterministic fallback")
        _write_final(deterministic, report_data)
        write_selection({"selected_source": "DETERMINISTIC", "llm_model": settings["model"], "guardrails_valid": False, "errors": ["LLM_PROVIDER_UNAVAILABLE"], "warnings": [], "fallback_used": True})
        return OUTPUT_DIR / "daily_report_final.md"

    logger.info("LLM narrative generation started")
    logger.info("LLM provider: %s", type(provider).__name__)
    try:
        narrative = provider.generate_narrative(payload)
        logger.info("LLM response received")
    except ProviderError as exc:
        logger.error("LLM provider failure: %s", str(exc))
        _write_final(deterministic, report_data)
        write_selection({"selected_source": "DETERMINISTIC", "llm_model": settings["model"], "provider_error": str(exc), "guardrails_valid": False, "errors": [str(exc)], "warnings": [], "fallback_used": True})
        return OUTPUT_DIR / "daily_report_final.md"
    except Exception as exc:
        logger.error("LLM provider failure: %s", type(exc).__name__)
        _write_final(deterministic, report_data)
        write_selection({"selected_source": "DETERMINISTIC", "llm_model": settings["model"], "guardrails_valid": False, "errors": ["LLM_PROVIDER_FAILURE"], "warnings": [], "fallback_used": True})
        return OUTPUT_DIR / "daily_report_final.md"

    logger.info("Guardrails validation started")
    result = validate_llm_output(narrative, report_data, payload)
    if result.valid:
        logger.info("Guardrails passed")
        (OUTPUT_DIR / "daily_report_llm.md").write_text(narrative, encoding="utf-8")
    else:
        logger.error("Guardrails failed: %s", ";".join(result.errors))
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / "rejected_llm_output.md").write_text(narrative, encoding="utf-8")

    selected = choose_publishable_report(narrative, result, deterministic)
    _write_final(selected["content"], report_data)
    write_selection({"selected_source": selected["selected_source"], "llm_model": settings["model"], "guardrails_valid": result.valid, "errors": result.errors, "warnings": result.warnings, "fallback_used": selected["selected_source"] == "DETERMINISTIC"})
    logger.info("Narrative pipeline completed in %.3f seconds", time.monotonic() - started)
    return OUTPUT_DIR / "daily_report_final.md"


def main() -> None:
    """Run the configured optional narrative pipeline."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s llm - %(message)s")
    run_narrative()


if __name__ == "__main__":
    main()
