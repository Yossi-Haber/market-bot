"""Synthetic tests for the provider-neutral narrative safety boundary."""

import copy
import json
from pathlib import Path

from llm.guardrails import choose_publishable_report, validate_llm_output
from llm.input_builder import build_llm_payload


FIXTURE = Path(__file__).parent / "fixtures" / "report_data.json"


def fixture_data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def setup_payload():
    data = fixture_data()
    return data, build_llm_payload(data)


def test_public_payload_excludes_leads_and_embedded_catalysts():
    data, payload = setup_payload()
    assert "unconfirmed_leads" not in payload
    assert "catalyst" not in payload["movers"]["top_gainers"][0]
    assert len(payload["verified_stories"]) == len(data["verified_stories"]) == 0
    assert payload["macro"]["GOLD"]["symbol"] == "GOLD"
    assert payload["macro"]["GOLD"]["display_symbol"] == "GOLD"
    assert payload["macro"]["GOLD"]["display_name_he"] == "זהב"
    assert "XAU/USD" not in json.dumps(payload, ensure_ascii=False)


def test_gold_display_symbol_and_formatted_values_are_valid():
    data, payload = setup_payload()
    result = validate_llm_output("זהב עלה ב־0.27%", data, payload)
    assert result.valid


def test_provider_gold_symbol_is_rejected():
    data, payload = setup_payload()
    result = validate_llm_output("XAU/USD עלה ב־0.27%", data, payload)
    assert any(error.startswith("UNSUPPORTED_TICKER: XAU") for error in result.errors)


def test_display_formatted_spy_values_are_valid():
    data, payload = setup_payload()
    result = validate_llm_output("SPY עלה ב־0.54% ונסגר על 771.35", data, payload)
    assert result.valid


def test_different_spy_percentage_remains_invalid():
    data, payload = setup_payload()
    result = validate_llm_output("SPY עלה ב־0.64%", data, payload)
    assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM") for error in result.errors)


def test_entity_scoped_rounded_percentage_is_allowed_but_wrong_value_is_not():
    data = fixture_data()
    data["movers"]["top_losers"] = [{"symbol": "QCOM", "company_name": "Qualcomm", "close": 100.0, "change_pct": -7.17}]
    payload = build_llm_payload(data)
    assert validate_llm_output("QCOM ירדה בכ־7%.", data, payload).valid
    rejected = validate_llm_output("QCOM ירדה בכ־5%.", data, payload)
    assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM") for error in rejected.errors)


def test_us10y_display_values_are_valid():
    data, payload = setup_payload()
    result = validate_llm_output("תשואת US10Y עמדה על 5.18%, עלייה של +7 נ\"ב", data, payload)
    assert result.valid


def test_real_rejected_shape_is_valid():
    data, _ = setup_payload()
    data["sectors"]["positive_count"] = 8
    data["sectors"]["valid_count"] = 11
    data["sectors"]["valid_count"] = 11
    data["sectors"]["breadth"] = {"fraction": 8 / 11, "percentage": 72.7272727}
    payload = build_llm_payload(data)
    narrative = """השוק נסגר בעליות רחבות.

SPY — SPDR S&P 500 ETF: נסגר על 771.35, עלייה של +0.54%.
IWM — iShares Russell 2000 ETF: נסגר על 281.97, עלייה של +0.11%.

8 מתוך 11 קרנות הסקטור עלו.

זהב נסגר על 4,286.74, עלייה של 0.27%, נכון ל־25 בספטמבר 2026.

תשואת אג"ח ארה"ב ל־10 שנים: הנתון האחרון הזמין, נכון ל־24 בספטמבר 2026, עמד על 5.18%, עלייה של 7 נ"ב."""
    result = validate_llm_output(narrative, data, payload)
    assert result.valid, result.errors


def test_iso_date_slashes_do_not_create_numeric_claims():
    data, payload = setup_payload()
    result = validate_llm_output("הנתון האחרון זמין נכון ל־2026/09/28.", data, payload)
    assert result.valid, result.errors


def test_month_day_date_phrasing_does_not_create_numeric_claims():
    data, payload = setup_payload()
    for narrative in ("נכון ל־September 28.", "נכון ל־28 בספטמבר.", "נכון ל־28 בספטמבר 2026."):
        result = validate_llm_output(narrative, data, payload)
        assert result.valid, (narrative, result.errors)


def test_supported_count_matches_only_count_bucket():
    data, _ = setup_payload()
    data["sectors"]["positive_count"] = 8
    data["sectors"]["valid_count"] = 11
    payload = build_llm_payload(data)
    assert validate_llm_output("8 מתוך 11 סקטורים עלו.", data, payload).valid


def test_structured_sector_breadth_counts_are_semantically_supported():
    data, _ = setup_payload()
    data["sectors"].update({"positive_count": 3, "negative_count": 8, "valid_count": 11})
    payload = build_llm_payload(data)
    assert payload["sector_breadth"] == {"total": 11, "up": 3, "down": 8, "unchanged": 0}
    assert validate_llm_output("8 מתוך 11 קרנות הסקטור ירדו, לעומת 3 שעלו.", data, payload).valid
    assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM") for error in validate_llm_output("9 מתוך 11 קרנות הסקטור ירדו.", data, payload).errors)
    assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM") for error in validate_llm_output("8 מתוך 11 קרנות הסקטור ירדו, לעומת 4 שעלו.", data, payload).errors)


def test_unrelated_integer_does_not_use_sector_count_whitelist():
    data, _ = setup_payload()
    data["sectors"].update({"positive_count": 3, "negative_count": 8, "valid_count": 11})
    payload = build_llm_payload(data)
    result = validate_llm_output("SPY עלה ב־3 נקודות.", data, payload)
    assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM") for error in result.errors)


def test_no_catalyst_payload_hides_rejected_candidate_metadata_but_keeps_plausible_event():
    data, _ = setup_payload()
    data.update({
        "stock_catalysts": [
            {"driver_id": "STOCK_DASH", "related_symbols": ["DASH"], "verification_status": "NO_CATALYST_FOUND", "candidate_stories": [{"title": "A Look at DoorDash", "source_name": "GuruFocus"}]},
            {"driver_id": "STOCK_BA", "related_symbols": ["BA"], "verification_status": "PLAUSIBLE_NOT_VERIFIED", "selected_event": "FAA delays Boeing 737 Max 10 certification", "event_type": "REGULATORY", "causal_gate": "PASS"},
        ]
    })
    payload = build_llm_payload(data)
    assert "A Look at DoorDash" not in json.dumps(payload, ensure_ascii=False)
    assert "GuruFocus" not in json.dumps(payload, ensure_ascii=False)
    assert "FAA delays Boeing 737 Max 10 certification" in json.dumps(payload, ensure_ascii=False)


def test_percentage_and_date_numbers_are_not_confused():
    data, payload = setup_payload()
    assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM") for error in validate_llm_output("SPY עלה ב־8%.", data, payload).errors)
    assert validate_llm_output("SPY עלה ב־0.54% נכון ל־2026-09-28.", data, payload).valid


def test_numeric_rejection_logs_token_sentence_and_candidates(caplog):
    data, payload = setup_payload()
    with caplog.at_level("ERROR", logger="market_bot.guardrails"):
        result = validate_llm_output("SPY עלה ב־8%.", data, payload)
    assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM: 8") for error in result.errors)
    assert "token=8%" in caplog.text
    assert "sentence='SPY עלה ב־8%'" in caplog.text
    assert "candidate_matches=" in caplog.text
    assert "reason=NO_SEMANTIC_MATCH" in caplog.text


def test_real_output_absolute_change_shape_is_valid():
    data, _ = setup_payload()
    payload = build_llm_payload(data)
    narrative = """SPY נסגר על 771.35, עלייה של +4.17 נקודות ו־+0.54%.
QQQ עלה ב־+3.40 נקודות.
DIA עלה ב־+4.81 נקודות.
זהב עלה ב־+11.46 נקודות, שהם +0.27%.
VIX השתנה ב־-0.80.
WTI השתנה ב־-0.56.
DXY השתנה ב־-0.32.
Russell 2000."""
    result = validate_llm_output(narrative, data, payload)
    assert result.valid, result.errors


def test_decimal_tokenizer_does_not_create_plus_zero_false_positive():
    data, payload = setup_payload()
    result = validate_llm_output("IWM השתנה ב־+0.31.", data, payload)
    assert result.valid, result.errors


def test_display_names_do_not_trigger_ticker_or_forecast_false_positives():
    data, payload = setup_payload()
    assert validate_llm_output("SPY — SPDR S&P 500 ETF", data, payload).valid
    assert validate_llm_output("Russell 2000", data, payload).valid
    assert validate_llm_output("buyback", data, payload).valid
    assert any(error.startswith("UNSUPPORTED_FORECAST_OR_RECOMMENDATION") for error in validate_llm_output("SELL", data, payload).errors)
    assert any(error.startswith("UNSUPPORTED_FORECAST_OR_RECOMMENDATION") for error in validate_llm_output("Russell 2000 צפוי לעלות מחר.", data, payload).errors)


def test_unsupported_numeric_safety_remains_strict():
    data, payload = setup_payload()
    for narrative in ("SPY +4.54%", "SPY נסגר על 999.99.", "זהב +99.99", "Russell 3000"):
        result = validate_llm_output(narrative, data, payload)
        assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM") for error in result.errors), narrative


def test_valid_numbers_pass():
    data, payload = setup_payload()
    result = validate_llm_output("SPY עלה ב-0.54% ו-DIA עלה ב-0.94%.", data, payload)
    assert result.valid


def test_invalid_number_is_rejected():
    data, payload = setup_payload()
    result = validate_llm_output("SPY עלה ב-4.54%.", data, payload)
    assert any(error.startswith("UNSUPPORTED_NUMERIC_CLAIM") for error in result.errors)


def test_causal_claim_without_verified_story_is_rejected():
    data, payload = setup_payload()
    result = validate_llm_output("PYPL עלתה בעקבות רכישה.", data, payload)
    assert "UNVERIFIED_CAUSAL_CLAIM: PYPL" in result.errors


def test_unknown_ticker_is_rejected():
    data, payload = setup_payload()
    result = validate_llm_output("XYZ זינקה ב-5%.", data, payload)
    assert any(error.startswith("UNSUPPORTED_TICKER: XYZ") for error in result.errors)


def test_forecast_is_rejected():
    data, payload = setup_payload()
    result = validate_llm_output("NVDA צפויה לעלות מחר.", data, payload)
    assert any(error.startswith("UNSUPPORTED_FORECAST_OR_RECOMMENDATION") for error in result.errors)


def test_lagging_macro_current_claim_is_rejected():
    data, payload = setup_payload()
    result = validate_llm_output("WTI נסגר היום על 96.41", data, payload)
    assert "LAGGING_DATA_PRESENTED_AS_CURRENT: WTI" in result.errors


def test_lagging_macro_qualified_claim_passes():
    data, payload = setup_payload()
    result = validate_llm_output("נתון WTI האחרון הזמין הוא 96.41 נכון ל-2026-09-22.", data, payload)
    assert result.valid


def test_fallback_is_explicit():
    data, payload = setup_payload()
    result = validate_llm_output("SPY עלה ב-4.54%.", data, payload)
    selected = choose_publishable_report("unsafe", result, "deterministic")
    assert selected["selected_source"] == "DETERMINISTIC"
    assert selected["content"] == "deterministic"


def test_not_ready_payload_is_blocked():
    data = fixture_data()
    data["publication_readiness"]["status"] = "NOT_READY"
    try:
        build_llm_payload(data)
    except RuntimeError as exc:
        assert str(exc) == "REPORT_NOT_READY"
    else:
        raise AssertionError("NOT_READY payload was accepted")
