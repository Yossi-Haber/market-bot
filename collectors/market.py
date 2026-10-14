"""Twelve Data market data collection utilities."""

from __future__ import annotations

import logging
import time as monotonic_clock
from datetime import date, datetime, time, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import requests

from config import Instrument
from collectors.rate_limit import CreditRateLimitController, RateLimitWaitTimeout


TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
REQUEST_CONNECT_TIMEOUT_SECONDS = 5
REQUEST_READ_TIMEOUT_SECONDS = 20
REQUEST_DEADLINE_SECONDS = 55
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT_SECONDS, REQUEST_READ_TIMEOUT_SECONDS)


class TwelveDataClient:
    """Small synchronous client for Twelve Data daily time series."""

    def __init__(self, api_key: str, timeout: tuple[float, float] | float = REQUEST_TIMEOUT, rate_limiter: CreditRateLimitController | None = None) -> None:
        self.api_key = api_key
        self.timeout = (float(timeout), float(timeout)) if isinstance(timeout, (int, float)) else timeout
        self.rate_limiter = rate_limiter or CreditRateLimitController()
        self.request_count = 0
        self.successful_api_requests = 0
        self.retry_requests = 0

    def fetch_instrument(self, instrument: Instrument, logger: logging.Logger, market_date: str | None = None) -> dict[str, Any]:
        """Fetch and calculate the latest two completed daily bars."""
        symbol = instrument.symbol
        base = {
            "symbol": symbol,
            "display_name": instrument.display_name,
            "category": instrument.category,
            "source": "Twelve Data",
        }
        if not self.api_key or self.api_key == "PASTE_API_KEY_HERE":
            return self._failure(base, "MISSING_API_KEY")

        params = {
            "symbol": symbol,
            "interval": "1day",
            "outputsize": 10,
            "order": "DESC",
            "apikey": self.api_key,
        }
        if market_date:
            params["end_date"] = self._historical_end_date(market_date)
        logger.info("Twelve Data request started: %s", symbol)
        deadline: float | None = None
        rate_limit_waited = False
        for attempt in range(2):
            if attempt:
                self.retry_requests += 1
            try:
                set_pending = getattr(self.rate_limiter, "set_pending_request", None)
                if callable(set_pending):
                    set_pending(market_date=market_date, symbol=symbol, purpose="MARKET_DATA")
                waited = self.rate_limiter.before_request(logger)
                if waited:
                    rate_limit_waited = True
                    logger.info("WAIT_COMPLETE: Twelve Data quota wait complete; sending same request")
                if deadline is None or rate_limit_waited:
                    deadline = monotonic_clock.monotonic() + REQUEST_DEADLINE_SECONDS
                    rate_limit_waited = False
                remaining = deadline - monotonic_clock.monotonic()
                if remaining <= 0:
                    return self._failure(base, "REQUEST_DEADLINE_EXCEEDED")
                timeout = self._bounded_timeout(remaining)
                logger.info("HTTP attempt %d/2", attempt + 1)
                self.request_count += 1
                started = monotonic_clock.monotonic()
                request_started = getattr(self.rate_limiter, "request_started", None)
                request_finished = getattr(self.rate_limiter, "request_finished", None)
                request_id = request_started(market_date=market_date, symbol=symbol, purpose="MARKET_DATA", credits=1) if callable(request_started) else None
                try:
                    response = requests.get(TWELVE_DATA_URL, params=params, timeout=timeout)
                except requests.RequestException as exc:
                    if callable(request_finished):
                        request_finished(request_id, f"NETWORK_ERROR:{type(exc).__name__}")
                    raise
                if callable(request_finished):
                    request_finished(request_id, f"HTTP_{response.status_code}")
                self.rate_limiter.record_response(response, logger)
                if response.status_code == 429:
                    if attempt == 0:
                        self.rate_limiter.handle_rate_limit(logger)
                        rate_limit_waited = True
                        continue
                    return self._failure(base, "RATE_LIMITED")
                if response.status_code == 404:
                    logger.warning("Twelve Data symbol unavailable: %s; HTTP status=404", symbol)
                    return self._failure(base, "UNSUPPORTED_SYMBOL")
                if response.status_code == 403:
                    logger.warning("Twelve Data access restricted for %s; HTTP status=403", symbol)
                    return self._failure(base, "PLAN_RESTRICTED")
                response.raise_for_status()
            except RateLimitWaitTimeout:
                return self._failure(base, "RATE_LIMIT_WAIT_TIMEOUT")
            except RuntimeError as exc:
                return self._failure(base, "DAILY_QUOTA_EXHAUSTED")
            except requests.ConnectTimeout:
                elapsed = monotonic_clock.monotonic() - started
                logger.warning("HTTP timeout after %.1fs", elapsed)
                if attempt == 0:
                    continue
                logger.error("Twelve Data request failed: %s reason=CONNECT_TIMEOUT", symbol)
                return self._failure(base, "CONNECT_TIMEOUT")
            except requests.ReadTimeout:
                elapsed = monotonic_clock.monotonic() - started
                logger.warning("HTTP timeout after %.1fs", elapsed)
                if attempt == 0:
                    continue
                logger.error("Twelve Data request failed: %s reason=READ_TIMEOUT", symbol)
                return self._failure(base, "READ_TIMEOUT")
            except requests.Timeout:
                elapsed = monotonic_clock.monotonic() - started
                logger.warning("HTTP timeout after %.1fs", elapsed)
                if attempt == 0:
                    continue
                logger.error("Twelve Data request failed: %s reason=READ_TIMEOUT", symbol)
                return self._failure(base, "READ_TIMEOUT")
            except requests.RequestException as exc:
                safe_error = str(exc).replace(self.api_key, "[REDACTED]")
                return self._failure(base, f"NETWORK_ERROR: {safe_error}")

            try:
                payload = response.json()
            except ValueError:
                return self._failure(base, "MALFORMED_RESPONSE")

            if not isinstance(payload, dict):
                return self._failure(base, "MALFORMED_RESPONSE")
            if payload.get("status") == "error" or str(payload.get("code")) in {"401", "403", "429"}:
                message = self._safe_message(payload.get("message") or payload.get("code") or "API error")
                if str(payload.get("code")) == "429" or "rate" in message.lower() or "limit" in message.lower():
                    if attempt == 0:
                        self.rate_limiter.handle_rate_limit(logger)
                        continue
                    return self._failure(base, "RATE_LIMITED")
                return self._failure(base, self._classify_provider_error(message))
            break

        values = payload.get("values")
        if not isinstance(values, list):
            return self._failure(base, "PROVIDER_ERROR: MISSING_VALUES")

        bars = [bar for bar in values if self._valid_bar(bar)]
        bars.sort(key=lambda bar: self._bar_datetime(bar), reverse=True)
        bars = [bar for bar in bars if self._is_completed_bar(bar)]
        if market_date:
            bars = [bar for bar in bars if self._bar_datetime(bar).date().isoformat() <= market_date]
            if not bars or self._bar_datetime(bars[0]).date().isoformat() != market_date:
                failure = self._failure(base, "MISSING_EXACT_HISTORICAL_DATE")
                failure["_raw_dates"] = raw_dates
                return failure
        if len(bars) < 2:
            return self._failure(base, "PROVIDER_ERROR: FEWER_THAN_TWO_COMPLETED_BARS")

        latest, previous = bars[0], bars[1]
        close = float(latest["close"])
        previous_close = float(previous["close"])
        change = close - previous_close
        change_pct = ((close / previous_close) - 1) * 100
        if not isfinite(change_pct):
            return self._failure(base, "PROVIDER_ERROR: NON_FINITE_PERCENTAGE")

        self.successful_api_requests += 1
        logger.info("Request success: %s", symbol)

        return {
            **base,
            "close": round(close, 6),
            "previous_close": round(previous_close, 6),
            "change": round(change, 6),
            "change_pct": round(change_pct, 6),
            "open": self._number_or_none(latest.get("open")),
            "high": self._number_or_none(latest.get("high")),
            "low": self._number_or_none(latest.get("low")),
            "volume": self._number_or_none(latest.get("volume")),
            "latest_bar_datetime": str(latest["datetime"]),
            "completed_bars": [{"datetime": str(bar["datetime"]), "close": round(float(bar["close"]), 6)} for bar in bars],
            "verified": True,
            "data_status": "completed_daily_bar",
            **({"selected_source_date": str(latest["datetime"])[:10], "previous_source_date": str(previous["datetime"])[:10]} if market_date else {}),
        }

    def fetch_stock_batch(self, instruments: list[Instrument], logger: logging.Logger, market_date: str | None = None) -> dict[str, dict[str, Any]]:
        """Fetch one batch of stock daily series, costing one credit per symbol."""
        if not instruments:
            return {}
        if len(instruments) > 8:
            raise ValueError("stock batch size must not exceed the Basic-plan minute quota")
        bases = {
            instrument.symbol: {
                "symbol": instrument.symbol,
                "display_name": instrument.display_name,
                "category": "movers",
                "source": "Twelve Data",
            }
            for instrument in instruments
        }
        if not self.api_key or self.api_key == "PASTE_API_KEY_HERE":
            return {symbol: self._failure(base, "MISSING_API_KEY") for symbol, base in bases.items()}

        provider_symbols = [instrument.provider_symbol or instrument.symbol for instrument in instruments]
        params = {
            "symbol": ",".join(provider_symbols),
            "interval": "1day",
            "outputsize": 10,
            "order": "DESC",
            "apikey": self.api_key,
        }
        if market_date:
            # Twelve Data's daily end_date is an exclusive upper boundary:
            # end_date=D returns bars strictly before D. Request the next
            # calendar day, then apply the exact as-of-date filter below.
            params["end_date"] = self._historical_end_date(market_date)
        request_label = ",".join(instrument.symbol for instrument in instruments)
        logger.info("Twelve Data request started: %s", request_label)
        deadline: float | None = None
        rate_limit_waited = False
        for attempt in range(2):
            if attempt:
                self.retry_requests += 1
            try:
                set_pending = getattr(self.rate_limiter, "set_pending_request", None)
                if callable(set_pending):
                    set_pending(market_date=market_date, symbol=request_label, purpose="MOVER_FINAL")
                waited = self.rate_limiter.before_request(logger, credits_required=len(instruments))
                if waited:
                    rate_limit_waited = True
                    logger.info("WAIT_COMPLETE: Twelve Data batch quota wait complete; sending same batch")
                if deadline is None or rate_limit_waited:
                    deadline = monotonic_clock.monotonic() + REQUEST_DEADLINE_SECONDS
                    rate_limit_waited = False
                remaining = deadline - monotonic_clock.monotonic()
                if remaining <= 0:
                    logger.error("Twelve Data request failed: %s reason=REQUEST_DEADLINE_EXCEEDED", request_label)
                    return {symbol: self._failure(base, "REQUEST_DEADLINE_EXCEEDED") for symbol, base in bases.items()}
                timeout = self._bounded_timeout(remaining)
                logger.info("HTTP attempt %d/2", attempt + 1)
                logger.info("HTTP_ATTEMPT %d/2", attempt + 1)
                self.request_count += 1
                started = monotonic_clock.monotonic()
                request_started = getattr(self.rate_limiter, "request_started", None)
                request_finished = getattr(self.rate_limiter, "request_finished", None)
                request_id = request_started(market_date=market_date, symbol=request_label, purpose="MOVER_FINAL", credits=len(instruments)) if callable(request_started) else None
                try:
                    response = requests.get(TWELVE_DATA_URL, params=params, timeout=timeout)
                except requests.RequestException as exc:
                    if callable(request_finished):
                        request_finished(request_id, f"NETWORK_ERROR:{type(exc).__name__}")
                    raise
                if callable(request_finished):
                    request_finished(request_id, f"HTTP_{response.status_code}")
                self.rate_limiter.record_response(response, logger)
                logger.info("HTTP_RESPONSE status=%s symbols=%s", response.status_code, request_label)
                if response.status_code == 429:
                    if attempt == 0:
                        self.rate_limiter.handle_rate_limit(logger)
                        rate_limit_waited = True
                        continue
                    return {symbol: self._failure(base, "RATE_LIMITED") for symbol, base in bases.items()}
                response.raise_for_status()
            except RateLimitWaitTimeout:
                return {symbol: self._failure(base, "RATE_LIMIT_WAIT_TIMEOUT") for symbol, base in bases.items()}
            except RuntimeError:
                return {symbol: self._failure(base, "DAILY_QUOTA_EXHAUSTED") for symbol, base in bases.items()}
            except requests.ConnectTimeout:
                elapsed = monotonic_clock.monotonic() - started
                logger.warning("HTTP timeout after %.1fs", elapsed)
                if attempt == 0:
                    continue
                logger.error("Twelve Data request failed: %s reason=CONNECT_TIMEOUT", request_label)
                return {symbol: self._failure(base, "CONNECT_TIMEOUT") for symbol, base in bases.items()}
            except requests.ReadTimeout:
                elapsed = monotonic_clock.monotonic() - started
                logger.warning("HTTP timeout after %.1fs", elapsed)
                if attempt == 0:
                    continue
                logger.error("Twelve Data request failed: %s reason=READ_TIMEOUT", request_label)
                return {symbol: self._failure(base, "READ_TIMEOUT") for symbol, base in bases.items()}
            except requests.Timeout:
                elapsed = monotonic_clock.monotonic() - started
                logger.warning("HTTP timeout after %.1fs", elapsed)
                if attempt == 0:
                    continue
                logger.error("Twelve Data request failed: %s reason=READ_TIMEOUT", request_label)
                return {symbol: self._failure(base, "READ_TIMEOUT") for symbol, base in bases.items()}
            except requests.RequestException as exc:
                safe_error = str(exc).replace(self.api_key, "[REDACTED]")
                return {symbol: self._failure(base, f"NETWORK_ERROR: {safe_error}") for symbol, base in bases.items()}

            try:
                payload = response.json()
            except ValueError:
                return {symbol: self._failure(base, "MALFORMED_RESPONSE") for symbol, base in bases.items()}
            if not isinstance(payload, dict):
                return {symbol: self._failure(base, "MALFORMED_RESPONSE") for symbol, base in bases.items()}
            if payload.get("status") == "error" or str(payload.get("code")) == "429":
                if attempt == 0:
                    self.rate_limiter.handle_rate_limit(logger)
                    continue
                return {symbol: self._failure(base, "RATE_LIMITED") for symbol, base in bases.items()}
            break

        results: dict[str, dict[str, Any]] = {}
        for instrument, provider_symbol in zip(instruments, provider_symbols):
            item = payload.get(provider_symbol) or payload.get(instrument.symbol)
            if not isinstance(item, dict):
                missing = self._failure(bases[instrument.symbol], "UNSUPPORTED_SYMBOL")
                missing["_provider_returned"] = False
                results[instrument.symbol] = missing
                continue
            if item.get("status") == "error":
                message = str(item.get("message") or "PROVIDER_ERROR")[:300]
                provider_error = self._failure(bases[instrument.symbol], f"PROVIDER_ERROR: {message}")
                provider_error["_provider_returned"] = True
                results[instrument.symbol] = provider_error
                continue
            result = self._stock_result_from_payload(instrument, item, bases[instrument.symbol], market_date=market_date)
            result["_provider_returned"] = True
            results[instrument.symbol] = result
            if result.get("verified"):
                self.successful_api_requests += 1
        return results

    def _bounded_timeout(self, remaining: float) -> tuple[float, float]:
        """Keep each connect/read timeout finite and within the request deadline."""
        return (min(float(self.timeout[0]), remaining), min(float(self.timeout[1]), remaining))

    @staticmethod
    def _historical_end_date(market_date: str) -> str:
        """Return the Twelve Data boundary that includes ``market_date``."""
        return (date.fromisoformat(market_date) + timedelta(days=1)).isoformat()

    def _stock_result_from_payload(self, instrument: Instrument, payload: dict[str, Any], base: dict[str, Any], market_date: str | None = None) -> dict[str, Any]:
        values = payload.get("values")
        if not isinstance(values, list):
            return self._failure(base, "MISSING_VALUES")
        bars = [bar for bar in values if self._valid_bar(bar)]
        bars.sort(key=lambda bar: self._bar_datetime(bar), reverse=True)
        raw_dates = [str(bar["datetime"])[:10] for bar in bars]
        bars = [bar for bar in bars if self._is_completed_bar(bar)]
        if market_date:
            bars = [bar for bar in bars if self._bar_datetime(bar).date().isoformat() <= market_date]
            if not bars or self._bar_datetime(bars[0]).date().isoformat() != market_date:
                failure = self._failure(base, "MOVER_DATE_MISMATCH")
                failure["_raw_dates"] = raw_dates
                return failure
        if len(bars) < 2:
            failure = self._failure(base, "FEWER_THAN_TWO_COMPLETED_BARS")
            failure["_raw_dates"] = raw_dates
            return failure
        latest, previous = bars[0], bars[1]
        close = float(latest["close"])
        previous_close = float(previous["close"])
        change = close - previous_close
        change_pct = ((close / previous_close) - 1) * 100
        if not isfinite(change_pct):
            return self._failure(base, "NON_FINITE_PERCENTAGE")
        return {
            **base,
            "close": round(close, 6),
            "previous_close": round(previous_close, 6),
            "change": round(change, 6),
            "change_pct": round(change_pct, 6),
            "open": self._number_or_none(latest.get("open")),
            "high": self._number_or_none(latest.get("high")),
            "low": self._number_or_none(latest.get("low")),
            "volume": self._number_or_none(latest.get("volume")),
            "latest_bar_datetime": str(latest["datetime"]),
            "completed_bars": [{"datetime": str(bar["datetime"]), "close": round(float(bar["close"]), 6)} for bar in bars],
            "verified": True,
            "data_status": "completed_daily_bar",
            "_raw_dates": raw_dates,
            "_selected_previous_date": str(previous["datetime"])[:10],
            "selected_source_date": str(latest["datetime"])[:10] if market_date else None,
            "previous_source_date": str(previous["datetime"])[:10] if market_date else None,
        }

    @staticmethod
    def _valid_bar(bar: Any) -> bool:
        if not isinstance(bar, dict) or not bar.get("datetime"):
            return False
        try:
            raw_datetime = str(bar["datetime"]).replace("Z", "+00:00")
            datetime.fromisoformat(raw_datetime)
            close = float(bar.get("close"))
        except (TypeError, ValueError, KeyError):
            return False
        return isfinite(close) and close > 0

    @staticmethod
    def _bar_datetime(bar: dict[str, Any]) -> datetime:
        raw = str(bar["datetime"]).replace("Z", "+00:00")
        return datetime.fromisoformat(raw)

    def _is_completed_bar(self, bar: dict[str, Any]) -> bool:
        """Exclude today's daily candle until the US equity session has closed."""
        bar_date = self._bar_datetime(bar).date()
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        return bar_date < now_ny.date() or (
            bar_date == now_ny.date() and now_ny.time() >= time(16, 0)
        )

    @staticmethod
    def _failure(base: dict[str, Any], error: str) -> dict[str, Any]:
        return {
            **base,
            "verified": False,
            "error": error,
        }

    @staticmethod
    def _number_or_none(value: Any) -> float | int | None:
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return round(number, 6) if isfinite(number) else None

    def _safe_message(self, message: Any) -> str:
        return str(message).replace(self.api_key, "[REDACTED]")[:300]

    @staticmethod
    def _classify_provider_error(message: str) -> str:
        lower = message.lower()
        if any(term in lower for term in ("not found", "invalid symbol", "unsupported symbol", "symbol does not exist")):
            return "UNSUPPORTED_SYMBOL"
        if any(term in lower for term in ("plan", "permission", "subscription", "access denied")):
            return "PLAN_RESTRICTED"
        return f"PROVIDER_ERROR: {message}"
