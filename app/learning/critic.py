"""Advisory trade-review clients."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx

TRADE_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "failure_categories": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category": {"type": "string"},
                    "observation": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["category", "observation", "evidence"],
            },
        },
        "experiments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "scope": {"type": "string", "enum": ["ranking", "rejection"]},
                },
                "required": ["title", "hypothesis", "scope"],
            },
        },
    },
    "required": ["summary", "confidence", "failure_categories", "findings", "experiments"],
}


class TradeReviewClient(Protocol):
    model_name: str
    weekly_model_name: str

    def review(self, evidence: dict[str, Any]) -> dict[str, Any]: ...

    def synthesize(self, evidence: dict[str, Any]) -> dict[str, Any]: ...


class DisabledTradeReviewClient:
    model_name = "disabled"
    weekly_model_name = "disabled"

    def review(self, evidence: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("AI trade critic is disabled")

    def synthesize(self, evidence: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("AI weekly synthesis is disabled")


class OpenAITradeReviewClient:
    """OpenAI Responses API client with strict structured output and no tools."""

    def __init__(self, settings: Any):
        self.api_key = settings.learning_openai_api_key
        self.base_url = settings.learning_openai_base_url.rstrip("/")
        self.model_name = settings.learning_trade_critic_model
        self.weekly_model_name = settings.learning_weekly_synthesis_model
        self.timeout_seconds = int(settings.learning_openai_timeout_seconds)
        self.max_retries = int(settings.learning_openai_max_retries)

    def review(self, evidence: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            evidence=evidence,
            model_name=self.model_name,
            instruction=(
                "You are an advisory trading review critic. Analyze only the supplied "
                "sanitized evidence. Do not recommend changing risk limits, order size, "
                "brokers, or live execution. Propose bounded ranking/rejection experiments."
            ),
        )

    def synthesize(self, evidence: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            evidence=evidence,
            model_name=self.weekly_model_name,
            instruction=(
                "Synthesize recurring patterns across sanitized trade reviews. Produce only "
                "advisory ranking/rejection experiments. Never recommend changing risk limits, "
                "orders, broker routing, strategy code, or live execution."
            ),
        )

    def _request(
        self,
        *,
        evidence: dict[str, Any],
        model_name: str,
        instruction: str,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("LEARNING_OPENAI_API_KEY is not configured")
        payload = {
            "model": model_name,
            "store": False,
            "tools": [],
            "input": [
                {
                    "role": "system",
                    "content": instruction,
                },
                {"role": "user", "content": json.dumps(evidence, sort_keys=True)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "trade_review",
                    "strict": True,
                    "schema": TRADE_REVIEW_SCHEMA,
                }
            },
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.post(
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"OpenAI Responses API failed: HTTP {response.status_code}")
                return json.loads(_response_text(response.json()))
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 4))
        raise RuntimeError(str(last_error or "OpenAI trade review failed"))


def _response_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    for output in payload.get("output") or []:
        for content in output.get("content") or []:
            if content.get("text"):
                return str(content["text"])
    raise RuntimeError("OpenAI response did not contain structured output text")


def sanitize_evidence(value: Any) -> Any:
    """Remove secrets and account identifiers before external review."""

    forbidden = ("secret", "token", "api_key", "apikey", "authorization", "account_number", "account_id")
    if isinstance(value, dict):
        return {
            str(key): sanitize_evidence(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in forbidden)
        }
    if isinstance(value, list):
        return [sanitize_evidence(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:2000]
    return value


def build_trade_review_client(settings: Any) -> TradeReviewClient:
    if bool(settings.learning_openai_enabled) and bool(settings.learning_reviews_enabled):
        return OpenAITradeReviewClient(settings)
    return DisabledTradeReviewClient()
