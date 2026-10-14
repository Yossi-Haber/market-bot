"""Optional OpenAI Responses API adapter with one bounded generation attempt."""

from __future__ import annotations

from typing import Any

try:
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        NotFoundError,
        OpenAI,
        PermissionDeniedError,
        RateLimitError,
    )
except ImportError:  # pragma: no cover - requirements install supplies the SDK.
    OpenAI = None
    APIConnectionError = APIStatusError = APITimeoutError = AuthenticationError = BadRequestError = NotFoundError = PermissionDeniedError = RateLimitError = ()

from ..prompt import build_prompt
from .base import LLMProvider, ProviderError


class OpenAIProvider(LLMProvider):
    """Generate one narrative using the official OpenAI Python SDK."""

    def __init__(self, api_key: str, model: str, timeout_seconds: int = 30, max_output_tokens: int = 900) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    def generate_narrative(self, payload: dict[str, Any]) -> str:
        """Call the Responses API once and return plain text, without logging secrets."""
        if not self.api_key:
            raise ProviderError("LLM_PROVIDER_UNAVAILABLE")
        try:
            if OpenAI is None:
                raise ProviderError("OPENAI_SDK_UNAVAILABLE")
            client = OpenAI(api_key=self.api_key, timeout=self.timeout_seconds, max_retries=0)
            response = client.responses.create(
                model=self.model,
                input=build_prompt(payload),
                max_output_tokens=self.max_output_tokens,
            )
            text = getattr(response, "output_text", "")
        except RateLimitError as exc:
            raise ProviderError(_classify_rate_limit(exc)) from exc
        except AuthenticationError as exc:
            raise ProviderError("OPENAI_AUTHENTICATION_FAILED") from exc
        except BadRequestError as exc:
            raise ProviderError("OPENAI_BAD_REQUEST") from exc
        except PermissionDeniedError as exc:
            raise ProviderError("OPENAI_ACCESS_DENIED") from exc
        except NotFoundError as exc:
            raise ProviderError("OPENAI_MODEL_NOT_FOUND") from exc
        except APITimeoutError as exc:
            raise ProviderError("OPENAI_TIMEOUT") from exc
        except APIConnectionError as exc:
            raise ProviderError("OPENAI_CONNECTION_ERROR") from exc
        except APIStatusError as exc:
            if getattr(exc, "status_code", None) == 429:
                raise ProviderError(_classify_rate_limit(exc)) from exc
            raise ProviderError("OPENAI_API_STATUS_ERROR") from exc
        except ProviderError:
            raise
        except Exception as exc:  # Unexpected SDK errors remain sanitized.
            raise ProviderError(f"OPENAI_PROVIDER_ERROR:{type(exc).__name__}") from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("EMPTY_LLM_RESPONSE")
        return text.strip()


def _api_error_code(exception: Any) -> str | None:
    """Read only the SDK's structured error code, never headers or full payloads."""
    body = getattr(exception, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
        if isinstance(body.get("code"), str):
            return body["code"]
    code = getattr(exception, "code", None)
    return code if isinstance(code, str) else None


def _classify_rate_limit(exception: Any) -> str:
    """Map a 429's safe API error code to a concise provider error."""
    mapping = {
        "credit_balance_exhausted": "OPENAI_CREDIT_BALANCE_EXHAUSTED",
        "organization_usage_limit_exceeded": "OPENAI_ORGANIZATION_USAGE_LIMIT_EXCEEDED",
        "organization_spend_limit_exceeded": "OPENAI_ORGANIZATION_SPEND_LIMIT_EXCEEDED",
        "project_spend_limit_exceeded": "OPENAI_PROJECT_SPEND_LIMIT_EXCEEDED",
        "insufficient_quota": "OPENAI_INSUFFICIENT_QUOTA",
    }
    code = _api_error_code(exception)
    return mapping.get(code, "OPENAI_RATE_LIMITED")
