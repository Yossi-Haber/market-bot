import pytest

from market_date import MarketDateResolutionError, resolve_market_date, select_as_of_bars


def core_dates(values):
    return {symbol: {"verified": True, "latest_bar_datetime": f"{value}T21:00:00"} for symbol, value in zip(("SPY", "QQQ", "DIA", "IWM"), values)}


def test_sunday_and_saturday_resolve_friday_from_core_consensus():
    result = resolve_market_date(core_dates(["2026-09-25"] * 4))
    assert result.market_date == "2026-09-25"
    assert result.source == "CORE_ETF_CONSENSUS" and result.agreement == "4/4"


def test_holiday_safe_three_of_four_consensus_warns():
    result = resolve_market_date(core_dates(["2026-11-25", "2026-11-25", "2026-11-25", "2026-11-24"]))
    assert result.market_date == "2026-11-25"
    assert result.agreement == "3/4" and result.warning == "CORE_ETF_CONSENSUS_3_OF_4"


def test_fewer_than_three_agreement_blocks_guessing():
    with pytest.raises(MarketDateResolutionError, match="BELOW_THRESHOLD"):
        resolve_market_date(core_dates(["2026-09-25", "2026-09-25", "2026-09-24", "2026-09-24"]))


def test_explicit_date_is_preserved_exactly():
    result = resolve_market_date(core_dates(["2026-09-25"] * 4), "2026-09-25")
    assert result.market_date == "2026-09-25" and result.source == "EXPLICIT"


def test_macro_and_gold_cannot_advance_canonical_date():
    result = resolve_market_date(core_dates(["2026-09-25"] * 4))
    assert result.market_date != "2026-09-26"
    gold = {"latest_bar_datetime": "2026-09-26T00:00:00", "completed_bars": [{"datetime": "2026-09-26T00:00:00", "close": 4300}, {"datetime": "2026-09-25T00:00:00", "close": 4286}, {"datetime": "2026-09-24T00:00:00", "close": 4270}]}
    selected = select_as_of_bars(gold, result.market_date)
    assert selected["latest_bar_datetime"].startswith("2026-09-25")
    assert selected["close"] == 4286


def test_matching_mover_date_is_accepted_by_single_canonical_date():
    result = resolve_market_date(core_dates(["2026-09-25"] * 4))
    mover_date = "2026-09-25"
    assert mover_date == result.market_date
