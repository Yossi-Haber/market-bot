"""Official Cboe VIX historical data collector."""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, time
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import requests

from config import Instrument


CBOE_VIX_CSV_URL = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
REQUEST_TIMEOUT_SECONDS = 20


class CboeClient:
    """Fetch daily VIX OHLC data from Cboe's official historical CSV."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self.request_count = 0
        self.successful_api_requests = 0

    def fetch_vix(self, instrument: Instrument, logger: logging.Logger, market_date: str | None = None) -> dict[str, Any]:
        """Return the latest two valid completed Cboe VIX rows."""
        base = {
            "symbol": instrument.symbol,
            "provider_symbol": instrument.provider_symbol or "VIX",
            "display_name": instrument.display_name,
            "category": instrument.category,
            "source": "CBOE",
        }
        logger.info("Requesting VIX from CBOE")
        self.request_count += 1
        try:
            response = requests.get(CBOE_VIX_CSV_URL, timeout=self.timeout)
            response.raise_for_status()
        except requests.Timeout:
            return self._failure(base, "CBOE_TIMEOUT")
        except requests.RequestException as exc:
            return self._failure(base, f"CBOE_PROVIDER_ERROR: {exc}")

        try:
            rows = list(csv.DictReader(io.StringIO(response.text)))
        except (csv.Error, UnicodeError):
            return self._failure(base, "CBOE_RESPONSE_FORMAT_CHANGED")
        observations = self._valid_rows(rows, market_date=market_date)
        if len(observations) < 2:
            return self._failure(base, "CBOE_RESPONSE_FORMAT_CHANGED")

        latest, previous = observations[0], observations[1]
        close, previous_close = latest["close"], previous["close"]
        change = close - previous_close
        change_pct = ((close / previous_close) - 1) * 100
        self.successful_api_requests += 1
        logger.info("CBOE VIX response successful")
        logger.info("Latest observation: %s; Previous observation: %s", latest["date"], previous["date"])
        return {
            **base,
            "close": round(close, 6),
            "previous_close": round(previous_close, 6),
            "change": round(change, 6),
            "change_pct": round(change_pct, 6),
            "open": latest.get("open"),
            "high": latest.get("high"),
            "low": latest.get("low"),
            "latest_observation_date": latest["date"].isoformat(),
            "previous_observation_date": previous["date"].isoformat(),
            "verified": True,
            **({"selected_source_date": latest["date"].isoformat(), "previous_source_date": previous["date"].isoformat(), "asof_lag_days": (date.fromisoformat(market_date) - latest["date"]).days} if market_date else {}),
        }

    @staticmethod
    def _valid_rows(rows: list[dict[str, Any]], *, market_date: str | None = None) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        for row in rows:
            normalized = {str(key).strip().upper(): value for key, value in row.items() if key is not None}
            try:
                raw_date = str(normalized.get("DATE", "")).strip()
                try:
                    row_date = date.fromisoformat(raw_date)
                except ValueError:
                    row_date = datetime.strptime(raw_date, "%m/%d/%Y").date()
                if row_date == now_ny.date() and now_ny.time() < time(16, 0):
                    continue
                if market_date and row_date.isoformat() > market_date:
                    continue
                close = float(normalized["CLOSE"])
                if not isfinite(close) or close <= 0:
                    continue
                parsed = {"date": row_date, "close": close}
                for field in ("OPEN", "HIGH", "LOW"):
                    value = normalized.get(field)
                    if value not in (None, ""):
                        number = float(value)
                        parsed[field.lower()] = round(number, 6) if isfinite(number) else None
                observations.append(parsed)
            except (KeyError, TypeError, ValueError):
                continue
        observations.sort(key=lambda row: row["date"], reverse=True)
        return observations

    @staticmethod
    def _failure(base: dict[str, Any], error: str) -> dict[str, Any]:
        return {**base, "verified": False, "error": error}
