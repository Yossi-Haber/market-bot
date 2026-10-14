"""Official FRED observations collector for macroeconomic series."""

from __future__ import annotations

import logging
from datetime import date
from math import isfinite
from typing import Any

import requests

from config import Instrument


FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
REQUEST_TIMEOUT_SECONDS = 15


class FredClient:
    """Fetch recent valid observations from the FRED API."""

    def __init__(self, api_key: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.request_count = 0
        self.successful_api_requests = 0

    def fetch_instrument(self, instrument: Instrument, logger: logging.Logger, market_date: str | None = None) -> dict[str, Any]:
        """Fetch two valid observations and normalize their daily movement."""
        series_id = instrument.provider_symbol or ""
        base = {
            "symbol": instrument.symbol,
            "provider_symbol": series_id,
            "display_name": instrument.display_name,
            "category": instrument.category,
            "source": "FRED",
        }
        if not self.api_key or self.api_key == "YOUR_FRED_API_KEY_HERE":
            return self._failure(base, "MISSING_API_KEY")

        logger.info("Requesting FRED series %s for %s", series_id, instrument.symbol)
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 10,
        }
        if market_date:
            params["observation_end"] = market_date
        self.request_count += 1
        try:
            response = requests.get(FRED_OBSERVATIONS_URL, params=params, timeout=self.timeout)
        except requests.Timeout:
            return self._failure(base, "TIMEOUT")
        except requests.RequestException as exc:
            safe_error = str(exc).replace(self.api_key, "[REDACTED]")
            return self._failure(base, f"NETWORK_ERROR: {safe_error}")

        try:
            payload = response.json()
        except ValueError:
            if response.status_code >= 400:
                return self._failure(base, f"PROVIDER_ERROR: HTTP_{response.status_code}")
            return self._failure(base, "MALFORMED_RESPONSE")
        if not isinstance(payload, dict):
            return self._failure(base, "MALFORMED_RESPONSE")
        if payload.get("error_code") or payload.get("error_message"):
            message = self._safe_message(payload.get("error_message") or payload.get("error_code"))
            return self._failure(base, f"PROVIDER_ERROR: {message[:300]}")
        if response.status_code >= 400:
            return self._failure(base, f"PROVIDER_ERROR: HTTP_{response.status_code}")

        observations = payload.get("observations")
        if not isinstance(observations, list):
            return self._failure(base, "PROVIDER_ERROR: MISSING_OBSERVATIONS")

        valid = self._valid_observations(observations)
        if market_date:
            valid = [item for item in valid if item[0].isoformat() <= market_date]
        if len(valid) < 2:
            return self._failure(base, "PROVIDER_ERROR: FEWER_THAN_TWO_VALID_OBSERVATIONS")

        latest_date, latest_value = valid[0]
        previous_date, previous_value = valid[1]
        change = latest_value - previous_value
        change_pct = ((latest_value / previous_value) - 1) * 100 if previous_value != 0 else None
        if not isfinite(change) or (change_pct is not None and not isfinite(change_pct)):
            return self._failure(base, "PROVIDER_ERROR: NON_FINITE_CHANGE")

        result = {
            **base,
            "close": round(latest_value, 6),
            "previous_close": round(previous_value, 6),
            "change": round(change, 6),
            "change_pct": round(change_pct, 6) if change_pct is not None else None,
            "latest_observation_date": latest_date.isoformat(),
            "previous_observation_date": previous_date.isoformat(),
            "verified": True,
            **({"selected_source_date": latest_date.isoformat(), "previous_source_date": previous_date.isoformat(), "asof_lag_days": (date.fromisoformat(market_date) - latest_date).days} if market_date else {}),
        }
        if instrument.symbol == "US10Y":
            result["change_bps"] = round(change * 100, 6)
        self.successful_api_requests += 1
        logger.info("FRED response successful for %s", instrument.symbol)
        logger.info("Latest observation: %s; Previous observation: %s", latest_date, previous_date)
        return result

    @staticmethod
    def _valid_observations(observations: list[Any]) -> list[tuple[date, float]]:
        valid: list[tuple[date, float]] = []
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            raw_date, raw_value = observation.get("date"), observation.get("value")
            if not raw_date or raw_value in (None, "", "."):
                continue
            try:
                observation_date = date.fromisoformat(str(raw_date))
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if isfinite(value):
                valid.append((observation_date, value))
        valid.sort(key=lambda item: item[0], reverse=True)
        return valid

    @staticmethod
    def _failure(base: dict[str, Any], error: str) -> dict[str, Any]:
        return {**base, "verified": False, "error": error}

    def _safe_message(self, message: Any) -> str:
        return str(message).replace(self.api_key, "[REDACTED]")[:300]
