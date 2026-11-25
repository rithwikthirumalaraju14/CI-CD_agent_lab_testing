import json

import httpx

from buildtrace.experiments.models import AgentMode, ModelSettings
from buildtrace.experiments.openai import OpenAIClient
from buildtrace.experiments.runner import run_trial

from experiment_fixtures import experiment_spec
from test_agent_modes import FakeBackend, patch, plan


def test_reasoning_state_is_preserved_within_a_trial_and_isolated_between_trials(tmp_path):
    spec = experiment_spec()
    spec.model = ModelSettings(model="gpt-6-luna", temperature=None, reasoning_effort="medium")
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        first = len(requests) % 2 == 1
        if first:
            assert len(body["input"]) == 2
            assert not any(item.get("type") == "reasoning" for item in body["input"])
            call = plan()["tool_calls"][0]
        else:
            assert any(item.get("encrypted_content") == "opaque-current-trial" for item in body["input"])
            assert body["input"][-1]["type"] == "function_call_output"
            assert body["input"][-1]["call_id"] == "call-plan"
            call = patch()["tool_calls"][0]
        function = {"type": "function_call", "id": "fc-1", "call_id": "call-plan" if first else "call-patch", **call["function"]}
        return httpx.Response(200, json={
            "id": f"resp-{len(requests)}", "model": "gpt-6-luna", "status": "completed",
            "output": [{"type": "reasoning", "id": "rs-1", "encrypted_content": "opaque-current-trial", "summary": []}, function],
            "usage": {"input_tokens": 100, "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 20}},
        })

    client = OpenAIClient(spec.model, api_key="test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    for trial in (0, 1):
        record = run_trial(spec, spec.tasks[0], AgentMode.NO_MEMORY, trial, FakeBackend(), client, tmp_path)
        assert record.success
        assert record.output_tokens == 100  # Reasoning is already included, so it is not added twice.
        assert record.reasoning_tokens == 40
        assert "opaque-current-trial" not in (tmp_path / record.trajectory_path).read_text(encoding="utf-8")
    assert len(requests) == 4
