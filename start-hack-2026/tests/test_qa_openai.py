import json
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from backend.qa_openai import ProviderError, Settings, provider


class PhraseChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: Literal["hello", "availability", "thanks"]


@pytest.fixture
def settings():
    return Settings(
        api_key="local-sdk-test", model="test-model", input_rate=2.0,
        output_rate=8.0, cached_rate=0.5,
    )


def _response(text, *, status="completed"):
    return {
        "id": "resp_local_test",
        "object": "response",
        "created_at": 0,
        "model": "test-model",
        "status": status,
        "output": [{
            "id": "msg_local_test",
            "type": "message",
            "role": "assistant",
            "status": status,
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 50},
            "output_tokens": 12,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 112,
        },
    }


def test_mock_uses_typed_choices_without_sdk_or_paid_cost(settings):
    model = provider(settings, mock=True)
    choice, usage = model("generate", {"action": "continue_conversation"}, PhraseChoice)
    assert choice.phrase == "availability"
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    assert settings.estimate(usage, mock=True) == 0
    assert settings.reserve("generate", {}, PhraseChoice, mock=True) == 0


def test_responses_api_sends_strict_schema_and_allowlisted_state_only(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unused.example")
    requests = []

    def simulate(request):
        requests.append(request)
        return httpx.Response(200, json=_response('{"phrase":"hello"}'))

    choose = provider(settings, mock=False, transport=httpx.MockTransport(simulate))
    context = {"action": "send_message", "target": "Private target profile", "reply": "Private reply"}
    choice, usage = choose("generate", context, PhraseChoice)
    assert choice.phrase == "hello"
    assert usage == {"input_tokens": 100, "output_tokens": 12, "cached_tokens": 50}
    assert settings.estimate(usage) == pytest.approx(0.000221)
    assert settings.reserve("generate", context, PhraseChoice) > settings.estimate(usage)

    request, = requests
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == "Bearer local-sdk-test"
    body = json.loads(request.content)
    assert body["store"] is False
    assert body["service_tier"] == "default"
    assert body["max_output_tokens"] == settings.max_output_tokens
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["additionalProperties"] is False
    assert json.loads(body["input"]) == {"action": "send_message"}


@pytest.mark.parametrize(("text", "status"), [
    ('{"phrase":"unapproved"}', "completed"),
    ('{"phrase":"hello","extra":"unapproved"}', "completed"),
    ('{"phrase":"hello"}', "incomplete"),
])
def test_failed_structured_outputs_still_return_usage(settings, text, status):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=_response(text, status=status)))
    choose = provider(settings, mock=False, transport=transport)
    with pytest.raises(ProviderError) as caught:
        choose("generate", {}, PhraseChoice)
    assert caught.value.usage["input_tokens"] == 100
    assert caught.value.may_have_cost
    assert "unapproved" not in str(caught.value)


@pytest.mark.parametrize("outcome", [429, 500, "timeout", "redirect"])
def test_errors_are_sanitized_and_sdk_does_not_retry(settings, outcome):
    requests = []

    def simulate(request):
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("private transport error")
        if outcome == "redirect":
            return httpx.Response(307, headers={"location": "https://unused.example"})
        return httpx.Response(outcome, json={"error": {"message": "private response error"}})

    choose = provider(settings, mock=False, transport=httpx.MockTransport(simulate))
    with pytest.raises(ProviderError) as caught:
        choose("generate", {}, PhraseChoice)
    assert len(requests) == 1
    assert "private" not in str(caught.value)
    assert caught.value.may_have_cost is (outcome in (500, "timeout"))


def test_settings_require_real_prices_and_reject_nonfinite_values(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-sdk-test")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.delenv("OPENAI_INPUT_USD_PER_MTOK", raising=False)
    assert not Settings.from_env().available
    monkeypatch.setenv("OPENAI_BUDGET_USD", "NaN")
    with pytest.raises(ValueError, match="OPENAI_BUDGET_USD"):
        Settings.from_env()
