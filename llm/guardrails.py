"""Deterministic checks for hypothetical LLM narratives; no provider calls."""

from __future__ import annotations

import re
import logging
from datetime import date
from typing import Any

from .schemas import GuardrailResult

MAX_NARRATIVE_LENGTH = 5000
CAUSAL_PHRASES = ("בגלל", "בעקבות", "לאחר ש", "על רקע", "כתוצאה מ", "after", "because", "following", "amid")
FORECAST_PHRASES = ("כדאי לקנות", "כדאי למכור", "צפויה לעלות", "צפויה לרדת", "צפוי לעלות", "צפוי לרדת", "יעד מחיר", "תקפוץ", "תקרוס", "will rise", "will fall", "buy", "sell", "price target")
QUALITY_PHRASES = ("הנתונים מלאים", "כל הנתונים מעודכנים", "כל החדשות אומתו", "all data is complete", "all data is current", "all news verified")
CURRENT_PHRASES = ("היום", "נכון להיום", "today", "current", "closed today", "נסגר היום")
KNOWN_NON_TICKERS = {"ETF", "LLM", "USD", "EUR", "THE", "AND", "FOR", "WITH", "DATA", "VIX", "DXY", "WTI", "US10Y", "GOLD"}
DISPLAY_ALIASES = {"GOLD": {"GOLD", "זהב"}}
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?|(?<![A-Za-z0-9])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?=\s*(?:bps|BPS|נ[\"״׳]?ב))")
TICKER_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,5}(?![A-Za-z0-9])")
DATE_RE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b")
HEBREW_DATE_RE = re.compile(r"\b(\d{1,2})\s+ב?(ינואר|פברואר|מרץ|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר)(?:\s+(\d{4}))?\b")
ENGLISH_DATE_RE = re.compile(r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)(?:\s+(\d{4}))?\b", re.IGNORECASE)
ENGLISH_MONTH_FIRST_DATE_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,?\s+(\d{4}))?\b", re.IGNORECASE)
LOGGER = logging.getLogger("market_bot.guardrails")
ENGLISH_FORECAST_RE = re.compile(r"(?<![A-Za-z])(buy|sell)(?![A-Za-z])", re.IGNORECASE)


def validate_llm_output(narrative_text: str, report_data: dict[str, Any], llm_payload: dict[str, Any], max_length: int = MAX_NARRATIVE_LENGTH) -> GuardrailResult:
    """Reject unsupported facts, unsafe causality, forecasts, and stale-data claims."""
    errors: list[str] = []
    warnings: list[str] = []
    if not narrative_text or not narrative_text.strip():
        errors.append("EMPTY_NARRATIVE")
    if len(narrative_text) > max_length:
        errors.append("NARRATIVE_TOO_LONG")
    errors.extend(validate_numbers(narrative_text, llm_payload))
    errors.extend(validate_tickers(narrative_text, llm_payload))
    errors.extend(validate_causal_language(narrative_text, report_data, llm_payload))
    errors.extend(validate_sources(narrative_text, report_data, llm_payload))
    forecast_phrase = forecast_match(narrative_text)
    if forecast_phrase:
        errors.append(f"UNSUPPORTED_FORECAST_OR_RECOMMENDATION: {forecast_phrase}")
    readiness = report_data.get("publication_readiness", {}).get("status")
    if readiness == "READY_WITH_WARNINGS" and any(phrase.lower() in narrative_text.lower() for phrase in QUALITY_PHRASES):
        errors.append("OVERSTATED_DATA_QUALITY")
    errors.extend(validate_macro_freshness(narrative_text, llm_payload))
    return GuardrailResult(valid=not errors, errors=sorted(set(errors)), warnings=warnings)


def numeric_values(payload: dict[str, Any]) -> tuple[list[float], list[float], list[float]]:
    """Return allowed values recursively extracted from public display fields."""
    values = numeric_claim_sets(payload)
    return values["percentages"], values["bps"], values["plain"]


def numeric_claim_sets(payload: dict[str, Any]) -> dict[str, list[float]]:
    """Build semantic numeric buckets from approved display fields and counts."""
    percentages: list[float] = []
    bps: list[float] = []
    changes: list[float] = []
    prices: list[float] = []
    labels: list[float] = []
    for key, text in collect_display_fields(payload):
        for match in re.finditer(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
            value = float(match.group(0).replace(",", ""))
            if key == "change_bps_display":
                bps.append(value)
            elif key.endswith("pct_display") or text.strip().endswith("%"):
                percentages.append(value)
            elif key == "change_display":
                changes.append(value)
            else:
                prices.append(value)
    for text in approved_display_text(payload):
        for match in re.finditer(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?", text):
            labels.append(float(match.group(0)))
    sectors = payload.get("sectors", {})
    breadth = sectors.get("breadth", {})
    if isinstance(breadth.get("percentage"), (int, float)):
        percentages.append(float(breadth["percentage"]))
    return {
        "percentages": percentages,
        "bps": bps,
        "changes": changes,
        "prices": prices,
        "labels": labels,
        "plain": changes + prices + labels,
    }


def structured_count_fields(payload: dict[str, Any]) -> dict[str, float]:
    """Return explicitly named deterministic counts, never a generic integer set."""
    breadth = payload.get("sector_breadth", {})
    if not isinstance(breadth, dict):
        breadth = {}
    result = {
        key: float(value)
        for key, value in breadth.items()
        if key in {"total", "up", "down", "unchanged"} and isinstance(value, (int, float))
    }
    # Backward-compatible input for callers that have not yet rebuilt the safe
    # payload, still retaining semantic field names.
    sectors = payload.get("sectors", {})
    legacy = {"total": sectors.get("valid_count"), "up": sectors.get("positive_count"), "down": sectors.get("negative_count")}
    for key, value in legacy.items():
        if key not in result and isinstance(value, (int, float)):
            result[key] = float(value)
    if "unchanged" not in result and all(key in result for key in ("total", "up", "down")):
        result["unchanged"] = result["total"] - result["up"] - result["down"]
    return result


def collect_display_fields(payload: Any, key: str = "") -> list[tuple[str, str]]:
    """Recursively collect approved presentation fields, never raw numeric fields."""
    fields: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for child_key, value in payload.items():
            if child_key.endswith("_display") and isinstance(value, str) and not child_key.endswith("date_display"):
                fields.append((child_key, value))
            fields.extend(collect_display_fields(value, child_key))
    elif isinstance(payload, list):
        for value in payload:
            fields.extend(collect_display_fields(value, key))
    return fields


def close_enough(value: float, candidates: list[float], *, allow_absolute: bool = False) -> bool:
    if any(abs(value - candidate) <= max(0.02, abs(candidate) * 0.005) for candidate in candidates):
        return True
    return allow_absolute and any(abs(abs(value) - abs(candidate)) <= max(0.02, abs(candidate) * 0.005) for candidate in candidates)


def validate_numbers(text: str, payload: dict[str, Any]) -> list[str]:
    values = numeric_claim_sets(payload)
    spans = approved_date_spans(text, payload)
    errors = []
    for match in NUMBER_RE.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        raw = match.group(0)
        value = float(raw.rstrip("%").replace(",", ""))
        context = text[match.end():match.end() + 8]
        is_bps = "bps" in context.lower() or re.search(r"נ[\"״׳]?ב", context) is not None
        if is_bps:
            candidates = values["bps"]
        elif raw.endswith("%"):
            candidates = values["percentages"]
        elif raw[:1] in "+-" or re.search(r"שינוי|עלייה|עלו|עלה|ירד|ירדה|השתנה|change|move", text[max(0, match.start() - 16):match.start()], re.IGNORECASE):
            candidates = values["changes"]
        elif re.search(r"נסגר על|עמד על|סגר על|close|closed|value", text[max(0, match.start() - 16):match.start()], re.IGNORECASE):
            candidates = values["prices"]
        else:
            candidates = values["plain"]
        sentence = _sentence_containing(text, match.start())
        structured_count = _structured_count_match(raw, text, match.start(), sentence, payload)
        if not close_enough(value, candidates, allow_absolute=raw.endswith("%")) and not structured_count and not _conservative_percentage_rounding(raw, sentence, payload, values):
            LOGGER.error(
                "UNSUPPORTED_NUMERIC_CLAIM token=%s sentence=%r normalized=%s candidate_matches=%s reason=%s",
                raw,
                sentence,
                value,
                candidates,
                "NO_SEMANTIC_MATCH",
            )
            errors.append(f"UNSUPPORTED_NUMERIC_CLAIM: {raw}")
    return errors


def _structured_count_match(raw: str, text: str, position: int, sentence: str, payload: dict[str, Any]) -> bool:
    """Match an integer only to its named sector-breadth semantic field."""
    if raw.endswith("%") or "." in raw or "," in raw:
        return False
    fields = structured_count_fields(payload)
    if not fields or not re.search(r"sector|סקטור|קרנות", sentence, re.IGNORECASE):
        return False
    try:
        value = float(raw)
    except ValueError:
        return False
    sentence_start = max(text.rfind(".", 0, position), text.rfind("!", 0, position), text.rfind("?", 0, position), text.rfind("\n", 0, position)) + 1
    local_position = position - sentence_start
    prefix = sentence[:local_position].lower()
    suffix = sentence[local_position + len(raw):].lower()
    if "total" in prefix or "סה" in prefix or "סה" in suffix or "מתוך" in prefix[-12:]:
        return value == fields.get("total")
    if "לעומת" in prefix[-24:] or "עלו" in suffix[:16] or "עלייה" in suffix[:16] or "up" in suffix[:16]:
        return value == fields.get("up")
    if any(term in suffix[:32] for term in ("ירד", "ירדו", "ירידה", "down", "negative")):
        return value == fields.get("down")
    if any(term in suffix[:32] for term in ("ללא שינוי", "נותרו ללא שינוי", "unchanged")):
        return value == fields.get("unchanged")
    return False


def _conservative_percentage_rounding(raw: str, sentence: str, payload: dict[str, Any], values: dict[str, list[float]]) -> bool:
    """Accept only an explicitly entity-scoped, nearby rounded percentage.

    This handles an LLM turning a display value such as ``-7.17%`` into
    ``-7%``/``7%`` while keeping unrelated invented percentages rejected.
    """
    if not raw.endswith("%") or "." in raw or "," in raw:
        return False
    tickers = set(TICKER_RE.findall(sentence))
    if not tickers:
        return False
    lowered = sentence.lower()
    negative_language = any(term in lowered for term in ("ירד", "ירדה", "ירדו", "נפלה", "ירידה", "fell", "down", "loss"))
    positive_language = any(term in lowered for term in ("עלה", "עלתה", "עלו", "עלייה", "rose", "up", "gain"))
    try:
        rounded = float(raw.rstrip("%"))
    except ValueError:
        return False
    for item in payload.get("movers", {}).get("top_gainers", []) + payload.get("movers", {}).get("top_losers", []):
        if not isinstance(item, dict) or str(item.get("symbol", "")).upper() not in tickers:
            continue
        expected_text = next((value for key, value in collect_display_fields(item) if key == "change_pct_display"), None)
        if not expected_text:
            continue
        expected_match = re.search(r"[-+]?\d+(?:\.\d+)?", expected_text.replace(",", ""))
        if not expected_match:
            continue
        expected = float(expected_match.group(0))
        if expected < 0 and not negative_language and not raw.startswith("-"):
            return False
        if expected > 0 and not positive_language and not raw.startswith("+"):
            return False
        if abs(abs(expected) - rounded) <= 0.25:
            return True
    return False


def _sentence_containing(text: str, position: int) -> str:
    """Return the complete bounded sentence containing a numeric token."""
    start = max(text.rfind(".", 0, position), text.rfind("!", 0, position), text.rfind("?", 0, position), text.rfind("\n", 0, position)) + 1
    ends = [index for index in (text.find(".", position), text.find("!", position), text.find("?", position), text.find("\n", position)) if index >= 0]
    end = min(ends) if ends else len(text)
    return text[start:end].strip()


def approved_display_text(payload: dict[str, Any]) -> list[str]:
    """Return approved labels whose embedded numbers/tokens are not security claims."""
    texts: list[str] = []
    for item in payload.get("indices", []) + payload.get("sectors", {}).get("all", []):
        texts.extend(str(item.get(key)) for key in ("display_name",) if item.get(key))
    for item in payload.get("movers", {}).get("top_gainers", []) + payload.get("movers", {}).get("top_losers", []):
        texts.extend(str(item.get(key)) for key in ("company_name",) if item.get(key))
    for item in payload.get("macro", {}).values():
        if isinstance(item, dict):
            texts.extend(str(item.get(key)) for key in ("display_name_he", "display_symbol") if item.get(key))
    return texts


def allowed_tickers(payload: dict[str, Any]) -> set[str]:
    symbols = set(KNOWN_NON_TICKERS)
    symbols.update(item.get("symbol") for item in payload.get("indices", []) if item.get("symbol"))
    symbols.update(item.get("symbol") for item in payload.get("sectors", {}).get("all", []) if item.get("symbol"))
    symbols.update(item.get("symbol") for item in payload.get("movers", {}).get("top_gainers", []) + payload.get("movers", {}).get("top_losers", []) if item.get("symbol"))
    symbols.update(payload.get("macro", {}).keys())
    symbols.update(item.get("symbol") for item in payload.get("verified_stories", []) if item.get("symbol"))
    symbols.update(symbol for item in payload.get("verified_stories", []) if isinstance(item, dict) for symbol in item.get("affected_symbols", []) if symbol)
    symbols.update(TICKER_RE.findall(" ".join(approved_display_text(payload))))
    for aliases in DISPLAY_ALIASES.values():
        symbols.update(alias for alias in aliases if alias.isupper())
    return {symbol for symbol in symbols if symbol}


def validate_tickers(text: str, payload: dict[str, Any]) -> list[str]:
    allowed = allowed_tickers(payload)
    return [f"UNSUPPORTED_TICKER: {token}" for token in sorted(set(TICKER_RE.findall(text))) if token not in allowed]


def forecast_match(text: str) -> str | None:
    """Find a complete forbidden phrase without matching words such as Russell or buyback."""
    for phrase in FORECAST_PHRASES:
        if phrase in {"buy", "sell"}:
            match = ENGLISH_FORECAST_RE.search(text)
            if match:
                return match.group(0)
        elif phrase.lower() in text.lower():
            return phrase
    return None


def allowed_dates(payload: dict[str, Any]) -> set[date]:
    """Collect dates explicitly present in the public-safe payload."""
    values: set[date] = set()
    candidates = [payload.get("market_date")]
    candidates.extend(item.get("latest_observation_date") for item in payload.get("macro", {}).values() if isinstance(item, dict))
    candidates.extend(str(item.get("latest_bar_datetime", ""))[:10] for item in payload.get("macro", {}).values() if isinstance(item, dict))
    for value in candidates:
        try:
            if value:
                values.add(date.fromisoformat(str(value)[:10]))
        except ValueError:
            continue
    return values


def approved_date_spans(text: str, payload: dict[str, Any]) -> list[tuple[int, int]]:
    """Find ISO, Hebrew, and English date forms that correspond to approved dates."""
    dates = allowed_dates(payload)
    spans: list[tuple[int, int]] = []
    months_he = {"ינואר": 1, "פברואר": 2, "מרץ": 3, "אפריל": 4, "מאי": 5, "יוני": 6, "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12}
    months_en = {name.lower(): index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
    for match in DATE_RE.finditer(text):
        try:
            parsed = date.fromisoformat(match.group(0).replace("/", "-"))
        except ValueError:
            continue
        # Dates are contextual metadata, not standalone financial claims.
        # Keep the span even when the date is not in the payload; numeric
        # validation is not the appropriate guardrail for date provenance.
        spans.append((max(0, match.start() - 1), match.end()))
    for regex, months in ((HEBREW_DATE_RE, months_he), (ENGLISH_DATE_RE, months_en)):
        for match in regex.finditer(text):
            month_name = match.group(2).lower() if months is months_en else match.group(2)
            try:
                year = int(match.group(3)) if match.group(3) else 2000
                parsed = date(year, months[month_name], int(match.group(1)))
            except (KeyError, TypeError, ValueError):
                continue
            if match.group(3) or 1 <= parsed.day <= 31:
                spans.append(match.span())
    for match in ENGLISH_MONTH_FIRST_DATE_RE.finditer(text):
        try:
            year = int(match.group(3)) if match.group(3) else 2000
            parsed = date(year, months_en[match.group(1).lower()], int(match.group(2)))
        except (KeyError, TypeError, ValueError):
            continue
        if match.group(3) or 1 <= parsed.day <= 31:
            spans.append(match.span())
    return spans


def verified_symbols(payload: dict[str, Any]) -> set[str]:
    symbols: set[str] = set()
    for item in payload.get("verified_stories", []):
        if not isinstance(item, dict) or item.get("verification") not in {None, "VERIFIED"}:
            continue
        if item.get("symbol"):
            symbols.add(item["symbol"])
        symbols.update(str(symbol) for symbol in item.get("affected_symbols", []) if symbol)
    return symbols


def validate_causal_language(text: str, report_data: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    verified = verified_symbols(payload)
    causality_supported = {
        str(symbol)
        for item in payload.get("verified_stories", [])
        if isinstance(item, dict) and item.get("causality_evidence") is True
        for symbol in ([item.get("symbol")] if item.get("symbol") else item.get("affected_symbols", []))
        if symbol
    }
    stock_symbols = {item.get("symbol") for item in payload.get("movers", {}).get("top_gainers", []) + payload.get("movers", {}).get("top_losers", [])}
    errors = []
    for sentence in re.split(r"[.!?\n]", text):
        if any(phrase.lower() in sentence.lower() for phrase in CAUSAL_PHRASES):
            for symbol in sorted(stock_symbols.intersection(TICKER_RE.findall(sentence))):
                if symbol not in verified or symbol not in causality_supported:
                    errors.append(f"UNVERIFIED_CAUSAL_CLAIM: {symbol}")
    return errors


def validate_sources(text: str, report_data: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    verified_sources = {str(item.get("primary_source")) for item in payload.get("verified_stories", []) if item.get("primary_source")}
    candidate_sources = {"Quiver Quantitative", "GuruFocus", "MarketBeat", "AD HOC NEWS"}
    candidate_sources.update(str(item.get("primary_source")) for item in report_data.get("unconfirmed_leads", []) if item.get("primary_source"))
    return ["UNCONFIRMED_SOURCE_LEAK: " + source for source in sorted(candidate_sources) if source.lower() in text.lower() and source not in verified_sources]


def validate_macro_freshness(text: str, payload: dict[str, Any]) -> list[str]:
    errors = []
    lowered = text.lower()
    for symbol, item in payload.get("macro", {}).items():
        if item.get("freshness") in {"lagging", "stale"} and symbol.lower() in lowered:
            if any(phrase.lower() in lowered for phrase in CURRENT_PHRASES):
                errors.append(f"LAGGING_DATA_PRESENTED_AS_CURRENT: {symbol}")
    return errors


def choose_publishable_report(llm_text: str, guardrail_result: GuardrailResult, deterministic_markdown: str) -> dict[str, str]:
    """Use LLM text only after validation; otherwise explicitly fall back."""
    if guardrail_result.valid:
        return {"selected_source": "LLM", "reason": "GUARDRAILS_PASSED", "content": llm_text}
    return {"selected_source": "DETERMINISTIC", "reason": ";".join(guardrail_result.errors) or "GUARDRAILS_FAILED", "content": deterministic_markdown}
