"""Opt-in OpenAI Responses transport. Imports and construction never call the API."""
from __future__ import annotations

import copy
import json
import threading
import time
import math
from dataclasses import dataclass
from decimal import Decimal

from .research import Completion


class ResearchTransportError(RuntimeError):
    """Sanitized machine-readable failure, never an SDK exception message."""


@dataclass(frozen=True)
class Pricing:
    model: str
    input_usd_per_million: str
    output_usd_per_million: str
    as_of: str
    source: str

    def cost(self, inputs, outputs):
        rates = [Decimal(self.input_usd_per_million), Decimal(self.output_usd_per_million)]
        if any(not r.is_finite() or r <= 0 for r in rates):
            raise ValueError("PRICING_INVALID")
        return (inputs * rates[0] + outputs * rates[1]) / Decimal(1000000)


def strict_schema(schema):
    """API schema requires every property, including A2's archive-optional claims."""
    result = copy.deepcopy(schema)
    def visit(node):
        if "const" in node:
            value = node.pop("const")
            node.update(type="boolean" if type(value) is bool else "integer" if type(value) is int else "string", enum=[value])
        if node.get("type") == "object":
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
            for child in node["properties"].values():
                visit(child)
        if "items" in node:
            visit(node["items"])
    visit(result)
    return result


def input_token_bound(payload, instructions, schema):
    # Conservative byte count plus framing allowance, not a tokenizer/billing quote.
    return len(json.dumps({"input": payload, "instructions": instructions, "schema": strict_schema(schema)}, ensure_ascii=False).encode()) + 4096


class OpenAIResearchProvider:
    def __init__(self, *, api_key, model, pricing: Pricing, max_spend_usd,
                 authorized=False, max_calls=1, max_input_tokens=50000,
                 max_output_tokens=4000, timeout_seconds=60, client=None):
        self.api_key, self.model, self.pricing = api_key, model, pricing
        self.authorized, self.client = authorized, client
        self.ceiling = Decimal(str(max_spend_usd))
        if not model or pricing.model != model or not self.ceiling.is_finite() or self.ceiling <= 0:
            raise ValueError("MODEL_OR_SPENDING_CONFIG_INVALID")
        if any(type(n) is not int or n <= 0 for n in (max_calls, max_input_tokens, max_output_tokens)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("RESEARCH_LIMITS_INVALID")
        pricing.cost(1, 1)
        self.max_calls, self.max_input_tokens, self.max_output_tokens = max_calls, max_input_tokens, max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.calls, self.reserved = 0, Decimal(0)
        self.lock = threading.Lock()
        self.last_text = ""
        self.usage = {"model_requested": model, "api_calls": 0, "simulated": False, "status": "NOT_CALLED", "pricing": pricing.__dict__.copy()}

    def complete(self, *, model, instructions, payload, schema, max_output_tokens, timeout_seconds):
        def deny(code):
            self.usage["status"] = code
            raise ResearchTransportError(code)
        if not self.authorized:
            deny("PAID_LLM_AUTHORIZATION_REQUIRED")
        if not self.api_key:
            deny("LLM_CREDENTIALS_UNAVAILABLE")
        if model != self.model or max_output_tokens > self.max_output_tokens:
            deny("MODEL_OR_OUTPUT_LIMIT_MISMATCH")
        inputs = input_token_bound(payload, instructions, schema)
        worst = self.pricing.cost(inputs, max_output_tokens)
        with self.lock:
            if self.calls >= self.max_calls or inputs > self.max_input_tokens:
                deny("LLM_TOKEN_OR_REQUEST_BUDGET_EXCEEDED")
            if self.reserved + worst > self.ceiling:
                deny("LLM_SPENDING_LIMIT_EXCEEDED")
            self.calls += 1
            self.reserved += worst  # Retain reservation on unknown outcome; never auto retry.
            self.usage.update(api_calls=self.calls, reserved_cost_usd=str(self.reserved), input_token_upper_estimate=inputs,
                              max_output_tokens=max_output_tokens, status="REQUEST_RESERVED")
        start = time.monotonic()
        owned_client = None
        try:
            from openai import OpenAI
            client = self.client or OpenAI(api_key=self.api_key, base_url="https://api.openai.com/v1", max_retries=0,
                                           timeout=min(timeout_seconds, self.timeout_seconds))
            if self.client is None:
                owned_client = client
            if str(client.base_url).rstrip("/") != "https://api.openai.com/v1":
                raise ResearchTransportError("OPENAI_ENDPOINT_NOT_PINNED")
            # Enforce the policy even for an injected official SDK client.
            client = client.with_options(max_retries=0, timeout=min(timeout_seconds, self.timeout_seconds))
            response = client.responses.create(model=model, instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False), max_output_tokens=max_output_tokens,
                text={"format": {"type": "json_schema", "name": "market_bot_v2_research", "strict": True, "schema": strict_schema(schema)}},
                store=False, truncation="disabled", service_tier="default")
            usage = response.usage
            if usage is None or any(type(v) is not int or v < 0 for v in (usage.input_tokens, usage.output_tokens)):
                raise ResearchTransportError("LLM_USAGE_MISSING_OR_INVALID")
            self.usage.update(model_returned=response.model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                              estimated_cost_usd=str(self.pricing.cost(usage.input_tokens, usage.output_tokens)), status=response.status)
            self.last_text = response.output_text or ""
            if usage.input_tokens > self.max_input_tokens or usage.output_tokens > max_output_tokens or self.pricing.cost(usage.input_tokens, usage.output_tokens) > worst:
                raise ResearchTransportError("LLM_REPORTED_USAGE_EXCEEDED_RESERVATION")
            refusal = any(getattr(c, "type", "") == "refusal" for o in response.output for c in getattr(o, "content", []))
            if refusal:
                raise ResearchTransportError("LLM_REFUSED")
            if response.status != "completed" or not self.last_text:
                raise ResearchTransportError("LLM_COMPLETION_INCOMPLETE")
            return Completion(self.last_text, response.status, usage.input_tokens, usage.output_tokens)
        except ResearchTransportError as exc:
            self.usage["status"] = str(exc)
            raise
        except Exception as exc:
            from openai import APITimeoutError, APIConnectionError, APIStatusError
            code = "LLM_TIMEOUT" if isinstance(exc, APITimeoutError) else "LLM_CONNECTION_FAILED" if isinstance(exc, APIConnectionError) else "LLM_API_ERROR" if isinstance(exc, APIStatusError) else "LLM_TRANSPORT_FAILED"
            self.usage.update(status=code, error_class=type(exc).__name__, http_status=getattr(exc, "status_code", None), outcome_cost_uncertain=True)
            raise ResearchTransportError(code) from None
        finally:
            self.usage["latency_seconds"] = time.monotonic() - start
            if owned_client is not None:
                try:
                    owned_client.close()
                except Exception:
                    self.usage["client_close_failed"] = True
