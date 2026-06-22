from __future__ import annotations

import json

from app.learning.critic import OpenAITradeReviewClient, sanitize_evidence
from tests.conftest import make_settings


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "output_text": json.dumps(
                {
                    "summary": "reviewed",
                    "confidence": 0.8,
                    "failure_categories": [],
                    "findings": [],
                    "experiments": [],
                }
            )
        }


def test_sanitize_evidence_removes_credentials_and_account_ids() -> None:
    sanitized = sanitize_evidence(
        {
            "symbol": "NVDA",
            "api_key": "secret",
            "nested": {"account_number": "PA123", "token": "secret", "price": 100},
        }
    )

    assert sanitized == {"symbol": "NVDA", "nested": {"price": 100}}


def test_openai_critic_uses_structured_responses_without_tools(tmp_path, monkeypatch) -> None:
    settings = make_settings(
        tmp_path,
        learning_reviews_enabled=True,
        learning_openai_enabled=True,
        learning_openai_api_key="test-key\n",
        learning_openai_max_retries=0,
    )
    captured = []

    def fake_post(url, **kwargs):
        captured.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr("app.learning.critic.httpx.post", fake_post)

    client = OpenAITradeReviewClient(settings)
    result = client.review({"symbol": "NVDA"})
    client.synthesize({"reviews": []})

    assert result["summary"] == "reviewed"
    assert captured[0]["headers"]["Authorization"] == "Bearer test-key"
    assert captured[0]["json"]["store"] is False
    assert captured[0]["json"]["tools"] == []
    assert captured[0]["json"]["text"]["format"]["strict"] is True
    assert captured[0]["json"]["model"] == "gpt-5.4-mini"
    assert captured[1]["json"]["model"] == "gpt-5.5"
