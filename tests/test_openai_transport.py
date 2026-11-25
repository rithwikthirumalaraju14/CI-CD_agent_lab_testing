import json

import httpx
import pytest

from buildtrace.experiments.models import ModelSettings
from buildtrace.experiments.openai import OpenAIClient, OpenAIError


def test_shared_transport_sends_fixed_settings_and_records_usage():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "model": "gpt-4.1-pinned", "id": "chat-1",
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "{}"}]}],
            "usage": {"input_tokens": 20, "output_tokens": 4},
        })

    client = OpenAIClient(ModelSettings(model="gpt-4.1"), api_key="local-test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    turn = client.complete([{"role": "user", "content": "task"}], max_tokens=100, tools=[{"type": "function", "function": {"name": "read"}}])
    payload = json.loads(requests[0].content)
    assert str(requests[0].url) == "https://api.openai.com/v1/responses"
    assert requests[0].headers["Authorization"] == "Bearer local-test-key"
    assert payload["model"] == "gpt-4.1" and payload["temperature"] == 0
    assert payload["parallel_tool_calls"] is False
    assert payload["max_output_tokens"] == 100
    assert payload["store"] is False
    assert payload["tools"][0]["name"] == "read"
    assert turn.input_tokens == 20 and turn.output_tokens == 4
    assert turn.returned_model == "gpt-4.1-pinned"


def test_no_key_or_missing_usage_cannot_produce_a_scored_live_run(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(OpenAIError, match="OPENAI_API_KEY"):
        OpenAIClient(ModelSettings(model="gpt-4.1"))
    client = OpenAIClient(ModelSettings(model="gpt-4.1"), api_key="test", client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"model": "gpt-4.1", "choices": [{"message": {"content": "done"}}]})
    )))
    with pytest.raises(OpenAIError, match="usage"):
        client.complete([], max_tokens=100)


def test_api_errors_do_not_echo_credentials():
    client = OpenAIClient(ModelSettings(model="gpt-4.1"), api_key="private-test-key", client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": "private-test-key"})
    )))
    with pytest.raises(OpenAIError) as error:
        client.complete([], max_tokens=100)
    assert "401" in str(error.value) and "private-test-key" not in str(error.value)


def test_luna_reasoning_parameters_and_output_limit():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "gpt-6-luna", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "id": "rs-1", "encrypted_content": "opaque", "summary": []}],
            "usage": {"input_tokens": 25, "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 100}},
        })

    settings = ModelSettings(model="gpt-6-luna", temperature=None, reasoning_effort="medium")
    client = OpenAIClient(settings, api_key="test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.complete([{"role": "user", "content": "task"}], max_tokens=100)
    assert "temperature" not in requests[0]
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert result.reasoning_tokens == result.output_tokens == 100
    assert result.stop_reason == "max_output_tokens"
    with pytest.raises(ValueError, match="temperature"):
        ModelSettings(model="gpt-6-luna", reasoning_effort="medium", temperature=0)
