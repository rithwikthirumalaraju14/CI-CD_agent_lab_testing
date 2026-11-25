import copy
import json

import pytest

from buildtrace.experiments.backend import PatchResult, changed_hunks
from buildtrace.experiments.models import AgentMode, EvidenceCitation, Verification
from buildtrace.experiments.openai import ModelTurn, OpenAIError
from buildtrace.experiments.runner import run_trial

from experiment_fixtures import experiment_spec


def tool(name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def plan(action="patch", summary="Use the correct total"):
    return tool("set_plan", {"action": action, "target": "src/invoice_lab/calculator.py", "parameters": {"base": "discounted"}, "summary": summary})


def patch():
    return tool("submit_patch", {"changes": [{"path": "src/invoice_lab/calculator.py", "content": "total = taxable_amount + tax\n"}], "explanation": "Preserve discount behavior"})


class ScriptedModel:
    def __init__(self, settings, script):
        self.settings, self.script, self.calls = settings, iter(script), []

    def complete(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        value = next(self.script)
        if isinstance(value, Exception):
            raise value
        return ModelTurn(value, 10, 5, "gpt-4.1-test-snapshot", {})


class FakeBackend:
    def __init__(self, passes=True):
        self.passes, self.patches = passes, 0

    def initial_observation(self):
        return {"base_sha": "c" * 40, "failed_jobs": [{"name": "invoice-checks", "steps": ["Run tests"]}]}

    def list_files(self):
        return ["src/invoice_lab/calculator.py"]

    def read_file(self, path):
        return "total = subtotal + tax\n"

    def read_log(self, *args):
        return {"text": "AssertionError: total differs"}

    def submit_patch(self, changes, *, timeout_seconds):
        self.patches += 1
        verification = Verification(
            attempt=self.patches, candidate_sha="e" * 40,
            run_url="https://github.com/example/invoice/actions/runs/23456/attempts/1",
            oracle_run_url="https://github.com/example/invoice/actions/runs/34567/attempts/1",
            workflow_conclusion="success" if self.passes else "failure",
            required_checks_passed=self.passes, protected_files_unchanged=True,
            oracle_passed=self.passes, elapsed_seconds=0,
        )
        hunks = changed_hunks({"src/invoice_lab/calculator.py": "total = subtotal + tax\n"}, {change["path"]: change["content"] for change in changes})
        return PatchResult(verification, hunks, {"passed": verification.passed})


class FakeMemory:
    sha256 = "a" * 64
    insights_sha256 = "b" * 64
    insights_text = "HISTORICAL-INSIGHT: check the original discount conditions."

    def __init__(self):
        self.reads = 0

    def assert_test_separation(self, spec):
        pass

    def read(self, insight_id, layer, document, offset, limit):
        self.reads += 1
        citation = EvidenceCitation(
            insight_id=insight_id, source_run_id="step-0000", layer=layer,
            document="/work/corpus/raw/step-0000/metadata.md", first_line=1, last_line=1,
            document_sha256="a" * 64,
        )
        return {"text": "ORIGINAL-EVIDENCE: the prior project used different discount conditions."}, citation


def test_same_solver_modes_share_insights_and_enforce_source_access(tmp_path):
    spec, memory = experiment_spec(), FakeMemory()
    users = {}
    for mode in AgentMode:
        model = ScriptedModel(spec.model, [plan(), patch()])
        record = run_trial(spec, spec.tasks[0], mode, 0, FakeBackend(), model, tmp_path, memory=memory)
        assert record.success
        assert record.model_calls == 2
        users[mode] = model.calls[0][0][1]["content"]
        available = {entry["function"]["name"] for entry in model.calls[0][1]["tools"]}
        assert ("read_historical_evidence" in available) == (mode == AgentMode.WIKITRACE)
    assert "HISTORICAL-INSIGHT" not in users[AgentMode.NO_MEMORY]
    assert users[AgentMode.INSIGHTS_ONLY] == users[AgentMode.WIKITRACE]
    assert memory.reads == 0


@pytest.mark.parametrize("mode", [AgentMode.NO_MEMORY, AgentMode.INSIGHTS_ONLY])
def test_forged_history_call_cannot_bypass_mode_policy(tmp_path, mode):
    spec, memory = experiment_spec(), FakeMemory()
    forged = tool("read_historical_evidence", {"insight_id": "insight-0000", "layer": "raw", "question": "What happened?", "trigger": "uncertainty"})
    model = ScriptedModel(spec.model, [forged, plan(), patch()])
    record = run_trial(spec, spec.tasks[0], mode, 0, FakeBackend(), model, tmp_path, memory=memory)
    assert record.success
    assert memory.reads == 0
    assert not record.evidence_decisions
    assert "not available" in model.calls[1][0][-1]["content"]


def test_wikitrace_records_auditable_before_after_action_change(tmp_path):
    spec, memory = experiment_spec(), FakeMemory()
    read = tool("read_historical_evidence", {"insight_id": "insight-0000", "layer": "raw", "question": "Does the previous fix apply?", "trigger": "environment-difference"})
    model = ScriptedModel(spec.model, [plan("apply"), read, plan("adapt"), patch()])
    record = run_trial(spec, spec.tasks[0], AgentMode.WIKITRACE, 0, FakeBackend(), model, tmp_path, memory=memory)
    assert record.success and memory.reads == 1
    assert len(record.evidence_decisions) == 1
    event = record.evidence_decisions[0]
    assert event.changed is True
    assert event.before.action == "apply" and event.after.action == "adapt"
    assert event.citations[0].document_sha256 == "a" * 64
    assert "ORIGINAL-EVIDENCE" in model.calls[2][0][-1]["content"]


def test_model_call_budget_and_provider_failure_are_distinct(tmp_path):
    spec = experiment_spec()
    spec.budget.model_calls = 2
    model = ScriptedModel(spec.model, [tool("list_current_files", {}), tool("list_current_files", {})])
    record = run_trial(spec, spec.tasks[0], AgentMode.NO_MEMORY, 0, FakeBackend(), model, tmp_path)
    assert record.outcome == "unsolved" and record.stop_reason == "model-call-budget"
    assert record.model_calls == 2
    model = ScriptedModel(spec.model, [OpenAIError("API quota unavailable")])
    record = run_trial(spec, spec.tasks[0], AgentMode.NO_MEMORY, 1, FakeBackend(), model, tmp_path)
    assert record.outcome == "infra-error"
    assert not record.success


def test_patch_budget_is_enforced_and_completed_trials_are_not_overwritten(tmp_path):
    spec = experiment_spec()
    spec.budget.patch_attempts = 1
    model = ScriptedModel(spec.model, [plan(), patch(), plan(), patch()])
    backend = FakeBackend(passes=False)
    record = run_trial(spec, spec.tasks[0], AgentMode.NO_MEMORY, 0, backend, model, tmp_path)
    assert record.stop_reason == "patch-budget" and backend.patches == 1
    with pytest.raises(FileExistsError):
        run_trial(spec, spec.tasks[0], AgentMode.NO_MEMORY, 0, backend, model, tmp_path)
