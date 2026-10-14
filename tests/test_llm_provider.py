"""Tests for provider abstraction and offline mock behavior."""

import pytest
import httpx2

from llm.providers import LLMProvider, MockProvider, ProviderError
from llm.providers import openai_provider
from llm.providers.openai_provider import OpenAIProvider


def test_mock_provider_implements_interface():
    provider = MockProvider("valid")
    assert isinstance(provider, LLMProvider)
    assert "SPY" in provider.generate_narrative({})
    assert provider.calls == 1


@pytest.mark.parametrize("mode", ["numeric", "causal", "ticker", "lagging"])
def test_mock_unsafe_modes_are_deterministic(mode):
    provider = MockProvider(mode)
    assert provider.generate_narrative({})
    assert provider.calls == 1


def test_mock_timeout_and_empty_response_modes():
    with pytest.raises(TimeoutError):
        MockProvider("timeout").generate_narrative({})
    assert MockProvider("empty").generate_narrative({}) == ""


def test_openai_provider_missing_key_fails_before_network():
    with pytest.raises(ProviderError, match="LLM_PROVIDER_UNAVAILABLE"):
        OpenAIProvider(api_key="", model="test").generate_narrative({})


class FailingResponses:
    def __init__(self, error):
        self.error = error

    def create(self, **kwargs):
        raise self.error


class FailingClient:
    def __init__(self, error):
        self.responses = FailingResponses(error)


def sdk_response(status: int):
    request = httpx2.Request("POST", "https://example.com")
    return httpx2.Response(status, request=request)


@pytest.mark.parametrize(
    "api_code, expected",
    [
        ("insufficient_quota", "OPENAI_INSUFFICIENT_QUOTA"),
        ("credit_balance_exhausted", "OPENAI_CREDIT_BALANCE_EXHAUSTED"),
        ("organization_usage_limit_exceeded", "OPENAI_ORGANIZATION_USAGE_LIMIT_EXCEEDED"),
        ("organization_spend_limit_exceeded", "OPENAI_ORGANIZATION_SPEND_LIMIT_EXCEEDED"),
        ("project_spend_limit_exceeded", "OPENAI_PROJECT_SPEND_LIMIT_EXCEEDED"),
        ("request_rate_limit", "OPENAI_RATE_LIMITED"),
    ],
)
def test_rate_limit_codes_are_sanitized(monkeypatch, api_code, expected):
    error = openai_provider.RateLimitError("safe message", response=sdk_response(429), body={"error": {"code": api_code}})
    monkeypatch.setattr(openai_provider, "OpenAI", lambda **kwargs: FailingClient(error))
    with pytest.raises(ProviderError, match=expected):
        OpenAIProvider(api_key="test-key", model="test").generate_narrative({})


def test_authentication_error_is_sanitized(monkeypatch):
    error = openai_provider.AuthenticationError("unauthorized", response=sdk_response(401), body=None)
    monkeypatch.setattr(openai_provider, "OpenAI", lambda **kwargs: FailingClient(error))
    with pytest.raises(ProviderError, match="OPENAI_AUTHENTICATION_FAILED"):
        OpenAIProvider(api_key="test-key", model="test").generate_narrative({})


def test_timeout_error_is_sanitized(monkeypatch):
    request = httpx2.Request("POST", "https://example.com")
    error = openai_provider.APITimeoutError(request)
    monkeypatch.setattr(openai_provider, "OpenAI", lambda **kwargs: FailingClient(error))
    with pytest.raises(ProviderError, match="OPENAI_TIMEOUT"):
        OpenAIProvider(api_key="test-key", model="test").generate_narrative({})
