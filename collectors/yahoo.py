"""Yahoo Finance chart collector for the delayed ICE Dollar Index symbol."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from config import Instrument


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
REQUEST_TIMEOUT_SECONDS = 15


class YahooClient:
    """Fetch completed daily bars from Yahoo Finance's chart endpoint."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self.request_count = 0
        self.successful_api_requests = 0

    def fetch_history_range(
        self,
        instrument: Instrument,
        start_date: str,
        end_date: str,
        logger: logging.Logger,
    ) -> dict[str, Any]:
        """Fetch one bounded Yahoo daily range and normalize dates in exchange time.

        Yahoo chart timestamps are epoch seconds. Daily timestamps are mapped
        through the exchange timezone reported in chart metadata, not the
        machine timezone or UTC calendar date.
        """
        symbol = instrument.provider_symbol or ""
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if end < start:
            return {"status": "INVALID_DATE", "error": "DATE_RANGE_REVERSED", "rows": []}
        request_url = YAHOO_CHART_URL.format(symbol=symbol)
        params = {
            "period1": int(datetime.combine(start, time.min, tzinfo=timezone.utc).timestamp()),
            "period2": int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc).timestamp()),
            "interval": "1d",
            "events": "history",
        }
        logger.info("Yahoo history range request: symbol=%s start=%s end=%s", symbol, start, end)
        self.request_count += 1
        try:
            response = requests.get(
                request_url,
                params=params,
                headers={"User-Agent": "market_bot/1.0", "Accept": "application/json"},
                timeout=(5, self.timeout),
            )
            status_code = response.status_code
            response.raise_for_status()
        except requests.Timeout as exc:
            return {"status": "SOURCE_ERROR", "error": f"YAHOO_TIMEOUT:{type(exc).__name__}", "http_status": getattr(locals().get("response"), "status_code", None), "rows": []}
        except requests.RequestException as exc:
            return {"status": "SOURCE_ERROR", "error": f"YAHOO_HTTP_ERROR:{getattr(locals().get('response'), 'status_code', None)}:{type(exc).__name__}", "http_status": getattr(locals().get("response"), "status_code", None), "rows": []}
        try:
            payload = response.json()
            result = payload["chart"]["result"][0]
            meta = result["meta"]
            exchange_timezone = str(meta["exchangeTimezoneName"])
            exchange_zone = ZoneInfo(exchange_timezone)
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError, ValueError, ZoneInfoNotFoundError):
            return {"status": "INVALID_SCHEMA", "error": "YAHOO_RESPONSE_FORMAT_CHANGED", "http_status": status_code, "rows": []}

        rows: list[dict[str, Any]] = []
        if not isinstance(timestamps, list):
            return {"status": "INVALID_SCHEMA", "error": "YAHOO_TIMESTAMP_ARRAY_MISSING", "http_status": status_code, "rows": []}
        for index, timestamp in enumerate(timestamps):
            try:
                source_timestamp = datetime.fromtimestamp(int(timestamp), timezone.utc)
                normalized_date = source_timestamp.astimezone(exchange_zone).date()
                if normalized_date < start or normalized_date > end:
                    continue
                values = {field: quote.get(field, [])[index] for field in ("open", "high", "low", "close", "volume") if index < len(quote.get(field, []))}
                rows.append({
                    "market_date": normalized_date.isoformat(),
                    "source_timestamp_utc": source_timestamp.isoformat(),
                    "source_timezone": exchange_timezone,
                    **{field: self._number(value) for field, value in values.items()},
                })
            except (IndexError, TypeError, ValueError, OverflowError):
                continue
        rows.sort(key=lambda row: row["market_date"])
        self.successful_api_requests += 1
        logger.info("Yahoo history range response: symbol=%s http_status=%s rows=%d timezone=%s", symbol, status_code, len(rows), exchange_timezone)
        return {"status": "AVAILABLE_VALID", "symbol": symbol, "source": "Yahoo Finance / ICE Futures delayed", "exchange_timezone": exchange_timezone, "requested_start": start.isoformat(), "requested_end": end.isoformat(), "http_status": status_code, "rows": rows}

    def fetch_instrument(self, instrument: Instrument, logger: logging.Logger, market_date: str | None = None) -> dict[str, Any]:
        """Return two completed daily bars for the configured Yahoo symbol."""
        symbol = instrument.provider_symbol or ""
        base = {
            "symbol": instrument.symbol,
            "provider_symbol": symbol,
            "display_name": instrument.display_name,
            "category": instrument.category,
            "source": "Yahoo Finance / ICE Futures delayed",
        }
        logger.info("Requesting DXY from Yahoo Finance")
        self.request_count += 1
        try:
            response = requests.get(
                YAHOO_CHART_URL.format(symbol=symbol),
                params={"range": "10d", "interval": "1d", "events": "history"},
                headers={"User-Agent": "market_bot/1.0"},
                timeout=self.timeout,
            )
            if response.status_code in {401, 403, 429}:
                return self._failure(base, f"YAHOO_ACCESS_FAILED: HTTP_{response.status_code}")
            response.raise_for_status()
        except requests.Timeout:
            return self._failure(base, "YAHOO_ACCESS_FAILED")
        except requests.RequestException:
            return self._failure(base, "YAHOO_ACCESS_FAILED")
        try:
            payload = response.json()
            chart = payload["chart"]
            chart_result = chart["result"][0]
            timestamps = chart_result["timestamp"]
            quote = chart_result["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError, ValueError):
            return self._failure(base, "YAHOO_RESPONSE_FORMAT_CHANGED")

        observations = self._observations(timestamps, quote, market_date=market_date)
        if len(observations) < 2:
            return self._failure(base, "YAHOO_RESPONSE_FORMAT_CHANGED")
        latest, previous = observations[0], observations[1]
        close, previous_close = latest["close"], previous["close"]
        change = close - previous_close
        change_pct = ((close / previous_close) - 1) * 100
        self.successful_api_requests += 1
        logger.info("Yahoo DXY response successful")
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
            "volume": latest.get("volume"),
            "latest_observation_date": latest["date"].isoformat(),
            "previous_observation_date": previous["date"].isoformat(),
            "verified": True,
            **({"selected_source_date": latest["date"].isoformat(), "previous_source_date": previous["date"].isoformat()} if market_date else {}),
        }

    @staticmethod
    def _observations(timestamps: Any, quote: dict[str, Any], *, market_date: str | None = None) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        if not isinstance(timestamps, list):
            return observations
        for index, timestamp in enumerate(timestamps):
            try:
                row_date = datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
                if market_date and row_date.isoformat() > market_date:
                    continue
                if row_date == now_ny.date() and now_ny.time() < time(16, 0):
                    continue
                close = YahooClient._number(quote["close"][index])
                if close is None or close <= 0:
                    continue
                row = {"date": row_date, "close": close}
                for field in ("open", "high", "low", "volume"):
                    values = quote.get(field, [])
                    row[field] = YahooClient._number(values[index]) if index < len(values) else None
                observations.append(row)
            except (IndexError, TypeError, ValueError):
                continue
        observations.sort(key=lambda row: row["date"], reverse=True)
        return observations

    @staticmethod
    def _number(value: Any) -> float | int | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return round(number, 6) if isfinite(number) else None

    @staticmethod
    def _failure(base: dict[str, Any], error: str) -> dict[str, Any]:
        return {**base, "verified": False, "error": error}
