"""Shared-solver integration and legacy identity checks; no model or HTTP calls."""

import hashlib
import importlib.metadata
import json
import time
from dataclasses import asdict
from threading import Barrier

import pytest
from wikitrace.memory.insights import InsightItem, render_inject

from buildtrace.evidence import EvidenceStore
from buildtrace.experiments import research
from buildtrace.experiments.backend import BackendError
from buildtrace.experiments.memory import FrozenMemory, UPSTREAM_REVISION
from buildtrace.experiments.models import (
    AgentMode, Budget, ExperimentSpec, ModelSettings, RetrievalSettings, TaskSpec, fingerprint,
)
from buildtrace.experiments.research import create_input_manifest, create_review_packages
from buildtrace.experiments.runner import run_trial
from buildtrace.experiments.workflow import apply_research_reviews, load_spec, run_experiment

from test_agent_modes import FakeBackend, ScriptedModel, patch, plan, tool
from conftest import log_zip


def test_original_pilot_identity_is_a_golden_v1_projection(tmp_path):
    common = {"split": "test", "repository": "bluepal-rithwik-thirumalaraju/buildtrace-ci-lab", "oracle_sha": "ab6ed493adf94149b45fd742e873cad8d2985447"}
    rows = [
        ("case-001", "discount-total", "test", "c7de58d8094fe4c2c5a952c0eb9888c417eb39f6", 43810145176, "e7b0a60aacfe4183a3dd9647390b5199", "0f5085e0a7addd7b965593b15f79792b531e931e62373dcd9c41fcd82c02ac15"),
        ("case-006", "dependency-project-range", "dependency", "80646c32a4285d438284ba80dc2d2a5f7505e054", 43820255328, "f911f70b8cbf4b009fb2edc228dfb53a", "c539b11d03ef2d51724422f50cd14a943d461a42b254b03f1056d0c7a581102d"),
        ("case-007", "module-shadowing", "import", "14edb49ccdb298a70e2ed6f73e6f81d113c35099", 43820332434, "c0eaf1662c5c4a9fbb195dbdc8168700", "1a02b2961558973c65619fa4be694f4a2bb58bdf313bfb4b5ae21c4ff8c5b18a"),
        ("case-008", "environment-invalid", "configuration", "03512488dc1d9c0a742b66d2273fcafdb0c2db25", 43820393967, "5653e2da9e474e72a2ddde41a79346d5", "41bdd5689ad60e9493d02b5218a52550bff08107b0689e2f1c8164e9dec9e019"),
    ]
    tasks = []
    for identifier, family, category, sha, run_id, capture, digest in rows:
        fields = dict(common, id=identifier, family=family, category=category, base_sha=sha,
                      initial_run_url=f"https://github.com/{common['repository']}/actions/runs/{run_id}/attempts/1",
                      capture_id=capture, capture_sha256=digest)
        if identifier == "case-007":
            fields["editable_paths"] = ["src/**/*.py", "pyproject.toml", "ci/constraints.txt", "pydantic.py"]
        if identifier == "case-008":
            fields.update(editable_paths=["src/**/*.py", "pyproject.toml", "ci/constraints.txt", ".github/workflows/ci.yml"],
                          protected_paths=["tests/", "ci/record_environment.py"])
        tasks.append(TaskSpec(**fields))
    spec = ExperimentSpec(
        id="invoice-evaluation-v2", model=ModelSettings(model="gpt-6-luna", temperature=None, reasoning_effort="medium"),
        budget=Budget(model_calls=20, patch_attempts=3, max_output_tokens=8192, total_tokens=80000, wall_seconds=1200),
        tasks=tasks, corpus=tmp_path / "arbitrary-local-corpus",
        corpus_sha256="a6f298ae32212e6669f09b8ec0caa0f93ec3323741a969c5e075e5ebdb8b9046",
        insights_sha256="3f40cd4133e5fb46c5c312d764585d6b843ce448326133b7f9dc5b6605e45423",
    )
    assert fingerprint(spec) == "47551127ab54756a7c6e6525cc267c3614dd5b4fcd878ae2fed4e9ef920fdaca"
    dumped = spec.model_dump(mode="json")
    assert "retrieval" not in dumped and "schema_version" not in dumped["tasks"][0]
    assert "artifact_prefix" not in dumped["tasks"][0]
    assert fingerprint(ExperimentSpec.model_validate_json(spec.model_dump_json())) == fingerprint(spec)


@pytest.fixture(scope="module")
def dependency_owners():
    return importlib.metadata.packages_distributions()


@pytest.fixture
def setup(tmp_path, collected_run, monkeypatch, dependency_owners):
    monkeypatch.setattr(research, "_dependency_snapshot", lambda names: {"roots": sorted(names), "distributions": {"fixture": {"version": "1"}}})
    monkeypatch.setattr(research.importlib.metadata, "packages_distributions", lambda: dependency_owners)
    store = EvidenceStore(tmp_path / "evidence")
    capture = store.save(collected_run)
    directory = tmp_path / "memory"
    directory.mkdir()
    items = [
        InsightItem(id=f"insight-{index:04d}", title="Expected total and discount" if index < 2 else "Unrelated zzyzx",
                    sections=(("Insight", "Compare expected total and discount conditions" if index < 2 else "quuxzz"),),
                    source_step=f"step-{index // 2:04d}", outcome="success", task="Historical repair", task_type="test")
        for index in range(4)
    ]
    files = {}
    for index in range(2):
        relative = f"raw/step-{index:04d}/solver.full.jsonl"
        content = (json.dumps({"type": "function_call", "name": "read_current_file", "is_error": False,
                               "result": {"path": "src/invoice_lab/calculator.py", "content": "The original total used the discounted amount."}}) + "\n").encode()
        path = directory / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        files[relative] = hashlib.sha256(content).hexdigest()
    manifest = {
        "schema_version": 1, "data_kind": "synthetic", "upstream": {"revision": UPSTREAM_REVISION},
        "compiler_model": ModelSettings(model="gpt-4.1").model_dump(mode="json"),
        "training_tasks": [{"id": f"historical-{index}", "family": f"historical-mechanism-{index}", "repository": "example/invoice",
                            "base_sha": str(index + 1) * 40, "source_run_id": f"history-{index}"} for index in range(2)],
        "insights": [asdict(item) for item in items], "files": files,
        "insights_sha256": hashlib.sha256(render_inject(items, source=True).encode()).hexdigest(),
    }
    (directory / "corpus.json").write_text(json.dumps(manifest), encoding="utf-8")
    memory = FrozenMemory(directory)
    task = TaskSpec(
        schema_version=2, id="development-primary", family="current-discount-boundary", split="dev", category="test",
        repository="example/invoice", base_sha=collected_run.source_ref, initial_run_url=capture["run_url"],
        capture_id=capture["id"], capture_sha256=fingerprint(capture), oracle_sha="f" * 40,
        artifact_prefix="bench-ci", expected_test_count=36, expected_oracle_test_count=12,
        verification_contract_sha256="e" * 64, oracle_contract_path="infra/research_v2/contract.json",
        editable_paths=["src/**/*.py", "pyproject.toml", "ci/constraints.txt"], editable_environment_keys=[],
        protected_paths=["tests/", ".github/"], required_steps={"checks": ["Run protected tests"]},
    )
    other = task.model_copy(update={"id": "development-secondary", "family": "current-rounding-policy"})
    spec = ExperimentSpec(
        schema_version=2, id="research-integration", data_kind="synthetic", model=ModelSettings(model="gpt-4.1"),
        budget=Budget(model_calls=20, total_tokens=200000), tasks=[task, other], retrieval=RetrievalSettings(),
        corpus=memory.directory, corpus_sha256=memory.sha256, insights_sha256=memory.insights_sha256,
    )
    phase = create_input_manifest(spec, "dev", memory=memory)
    spec = spec.model_copy(update={"phase_manifest_sha256": phase["sha256"]})
    packet = memory.select_packet(task, store, spec.retrieval)
    return spec, task, store, memory, phase, packet


def run(setup, tmp_path, mode, script, *, trial=0, backend=None):
    spec, task, _, memory, phase, packet = setup
    model = ScriptedModel(spec.model, script)
    record = run_trial(spec, task, mode, trial, backend or FakeBackend(), model, tmp_path,
                       memory=memory, packet=packet if mode != AgentMode.NO_MEMORY else None, phase_manifest=phase)
    return record, model


def history(insight="insight-0000"):
    return tool("read_historical_evidence", {"insight_id": insight, "layer": "raw", "document": "solver.full.jsonl",
                                           "question": "Which amount did the previous implementation actually use?", "trigger": "environment-difference"})


def test_v2_modes_have_identical_guidance_and_identical_selected_packets(setup, tmp_path):
    calls, records = {}, {}
    for mode in AgentMode:
        records[mode], model = run(setup, tmp_path, mode, [plan(), patch()])
        calls[mode] = model.calls[0]
    assert len({call[0][0]["content"] for call in calls.values()}) == 1
    shared_guidance = calls[AgentMode.WIKITRACE][0][0]["content"]
    assert "If read_historical_evidence is available" in shared_guidance
    assert 'layer="raw", document="solver.full.jsonl"' in shared_guidance
    assert "Compilation and metadata alone do not verify the original observation" in shared_guidance
    assert calls[AgentMode.INSIGHTS_ONLY][0][1] == calls[AgentMode.WIKITRACE][0][1]
    assert "<insight " not in calls[AgentMode.NO_MEMORY][0][1]["content"]
    first, second = records[AgentMode.INSIGHTS_ONLY], records[AgentMode.WIKITRACE]
    assert first.packet_sha256 == second.packet_sha256 == setup[-1].sha256
    assert first.packet_insight_ids == second.packet_insight_ids == ["insight-0000"]
    assert first.insights_sha256 == setup[3].insights_sha256 != first.packet_sha256
    common = calls[AgentMode.INSIGHTS_ONLY][1]["tools"]
    assert calls[AgentMode.WIKITRACE][1]["tools"][:-1] == common
    assert "original successful file/log observation" in calls[AgentMode.WIKITRACE][1]["tools"][-1]["function"]["description"]


def test_packet_query_ignores_operator_labels_and_preserves_whole_insights(setup):
    spec, task, store, memory, _, packet = setup
    changed = task.model_copy(update={"family": "different-operator-label", "category": "import"})
    selected = memory.select_packet(changed, store, spec.retrieval)
    assert (selected.query_sha256, selected.sha256, selected.insight_ids) == (packet.query_sha256, packet.sha256, packet.insight_ids)
    assert selected.task_sha256 != packet.task_sha256
    empty = memory.select_packet(task, store, RetrievalSettings(max_characters=1))
    assert empty.text == "" and empty.insight_ids == () and empty.sha256 == hashlib.sha256(b"").hexdigest()
    with pytest.raises(ValueError, match="assigned insight packet"):
        memory.read("insight-0001", "raw", allowed_insight_ids=packet.insight_ids)


def test_literal_history_search_returns_only_a_cited_original_observation(setup):
    _, _, _, memory, _, packet = setup
    relative = "raw/step-0000/solver.full.jsonl"
    source = memory.directory / relative
    rows = [
        {"type": "message", "role": "assistant", "content": "secret guess: Client(token)"},
        {"type": "function_call", "name": "read_current_file", "is_error": True,
         "result": {"path": "src/client.py", "content": "Client(token)"}},
        {"type": "function_call", "name": "read_current_file", "is_error": False,
         "result": {"path": "src/client.py", "content": "Observed API: Client(token, timeout=5)"}},
    ]
    journal = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    source.write_bytes(journal.encode("utf-8"))
    manifest_path = memory.directory / "corpus.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][relative] = hashlib.sha256(journal.encode("utf-8")).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    refreshed = FrozenMemory(memory.directory)

    result, citation = refreshed.read(
        "insight-0000", "raw", needle="Client(", limit=200,
        allowed_insight_ids=packet.insight_ids,
    )
    assert "Observed API: Client(token, timeout=5)" in result["text"]
    assert "secret guess" not in result["text"]
    assert research._journal_observation(journal, result["offset"], len(result["text"]))
    assert citation.document.endswith(relative)
    assert citation.document_sha256 == manifest["files"][relative]
    with pytest.raises(ValueError, match="No linked original"):
        refreshed.read("insight-0000", "raw", needle="secret guess")
    with pytest.raises(ValueError, match="assigned insight packet"):
        refreshed.read("insight-0001", "raw", needle="Client(", allowed_insight_ids=packet.insight_ids)


def test_diagnostic_retrieval_ignores_successful_setup_and_secondary_failure_noise(setup, collected_run):
    spec, task, store, memory, _, _ = setup
    collected_run.jobs[0]["steps"].append({"name": "Verify evidence", "number": 12, "conclusion": "failure"})
    collected_run.log_archive = log_zip({
        "invoice-checks/5_Provision tools.txt": b"unrelated quuxzz bootstrap-only dependency versions\n" * 50,
        "invoice-checks/8_Run tests.txt": b"2026-11-17T00:00:00Z ##[group]Run python verifier\n2026-11-17T00:00:00Z env:\n2026-11-17T00:00:00Z   BOOTSTRAP: quuxzz\n2026-11-17T00:00:00Z ##[endgroup]\n2026-11-17T00:00:00Z AssertionError: expected total after discount\n",
        "invoice-checks/12_Verify evidence.txt": b"Unrelated quuxzz downstream-only proof failure\n",
    })
    capture = store.save(collected_run)
    task = task.model_copy(update={"capture_id": capture["id"], "capture_sha256": fingerprint(capture)})
    settings = RetrievalSettings(query="initial-failed-step-diagnostics-v2", tokenizer="diagnostic-identifiers-v2", max_insights=1)
    packet = memory.select_packet(task, store, settings)
    assert packet.insight_ids == ("insight-0000",)
    from buildtrace.experiments.memory import _initial_query, _tokens
    query = _initial_query(capture, store, settings.query)
    assert "expected total after discount" in query
    assert "bootstrap-only" not in query and "downstream-only" not in query and "quuxzz" not in query
    assert "bootstrap-only" in _initial_query(capture, store, "initial-failed-logs-and-steps-v1")
    assert {"status", "response", "none"} <= set(_tokens("response.status is None", settings.tokenizer))


def test_diagnostic_packets_remain_byte_identical_across_memory_modes(setup, tmp_path):
    spec, task, store, memory, _, _ = setup
    settings = RetrievalSettings(query="initial-failed-step-diagnostics-v2", tokenizer="diagnostic-identifiers-v2")
    spec = spec.model_copy(update={"retrieval": settings, "phase_manifest_sha256": None})
    phase = create_input_manifest(spec, "dev", memory=memory)
    spec = spec.model_copy(update={"phase_manifest_sha256": phase["sha256"]})
    state = (spec, task, store, memory, phase, memory.select_packet(task, store, settings))
    calls = []
    for mode in (AgentMode.INSIGHTS_ONLY, AgentMode.WIKITRACE):
        record, model = run(state, tmp_path, mode, [plan(), patch()])
        assert record.success and record.packet_query_sha256 == state[-1].query_sha256
        calls.append(model.calls[0][0])
    assert calls[0] == calls[1]


def test_forged_nonpacket_request_is_logged_without_delivering_source(setup, tmp_path, monkeypatch):
    memory = setup[3]
    monkeypatch.setattr(memory, "read", lambda *args, **kwargs: pytest.fail("Source boundary was bypassed"))
    record, _ = run(setup, tmp_path, AgentMode.WIKITRACE, [plan(), history("insight-0001"), plan(), patch()])
    assert record.success and not record.evidence_decisions
    assert len(record.evidence_reads) == 1 and record.evidence_reads[0].status == "error"
    assert "assigned insight packet" in record.evidence_reads[0].error


def test_delivered_raw_source_links_exact_plans_and_adopted_patch_and_review(setup, tmp_path):
    record, _ = run(setup, tmp_path, AgentMode.WIKITRACE, [plan("apply"), history(), plan("adapt"), patch()])
    event, read = record.evidence_decisions[0], record.evidence_reads[0]
    assert read.status == "delivered" and read.delivered_characters > 0
    assert read.id == event.read_ids[0]
    before = next(action for action in record.actions if action.id == event.before_action_id)
    after = next(action for action in record.actions if action.id == event.after_action_id)
    assert before.decision == event.before and after.decision == event.after
    assert record.actions[-1].decision == event.after and record.actions[-1].verification_attempt == 1
    original = (record.model_dump_json() + "\n").encode()
    hunks, evidence, mapping = create_review_packages([record], original, manifest=setup[4], memory=setup[3])
    assert evidence["items"][0]["reads"][0]["replay"]["original_observation"]
    assert record.id not in json.dumps(hunks)
    annotation = {
        "hunk_package_sha256": hunks["sha256"], "evidence_package_sha256": evidence["sha256"],
        "hunks": [{"item_id": item["item_id"], "classification": "necessary", "reviewer": "blind-reviewer"} for item in hunks["items"]],
        "evidence": [{"item_id": evidence["items"][0]["item_id"], "material_effect": "refinement", "adoption": "adopted",
                      "adoption_action_ids": [record.actions[-1].id], "contaminated": False, "mechanism_reviewer": "separate-reviewer"}],
    }
    reviewed = apply_research_reviews([record], annotation, mapping, original)[0]
    assert reviewed.evidence_decisions[0].adoption == "adopted"
    assert record.evidence_decisions[0].adoption == "unreviewed"
    assert all(hunk.classification == "necessary" for hunk in reviewed.hunks)
    annotation["evidence"][0]["after"] = {"fabricated": True}
    with pytest.raises(ValueError, match="only contain review labels"):
        apply_research_reviews([record], annotation, mapping, original)


def test_intervening_current_observation_contaminates_a_source_burst(setup, tmp_path):
    current = tool("read_current_file", {"path": "src/invoice_lab/calculator.py"})
    record, _ = run(setup, tmp_path, AgentMode.WIKITRACE, [plan("apply"), history(), current, history(), plan("adapt"), patch()])
    event = record.evidence_decisions[0]
    assert event.contaminated and event.after is None
    assert [read.status for read in record.evidence_reads] == ["delivered", "error"]
    assert "fresh set_plan" in record.evidence_reads[-1].error


def test_failed_verification_retains_hunks_capture_metadata_and_ci_time_once(setup, tmp_path):
    class BrokenArchiveBackend(FakeBackend):
        def submit_patch(self, changes, *, timeout_seconds):
            result = super().submit_patch(changes, timeout_seconds=timeout_seconds)
            time.sleep(0.01)
            error = BackendError("Archive publication failed", elapsed_seconds=0.005, diagnostics={"phase": "archive"})
            error.verification, error.hunks = result.verification, result.hunks
            error.observation = {"current_logs": [{"entry_id": "retained/log"}]}
            raise error

    record, _ = run(setup, tmp_path, AgentMode.NO_MEMORY, [plan(), patch()], backend=BrokenArchiveBackend(passes=False))
    assert record.outcome == "infra-error" and record.timing.ci_wait_seconds == 0.005
    assert record.hunks and len(record.verifications) == record.patch_attempts == 1
    trajectory = tmp_path / record.trajectory_path
    rows = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines()]
    failure = next(row for row in rows if row["type"] == "infrastructure_error")
    assert failure["diagnostics"]["phase"] == "archive" and failure["observation"]["current_logs"]


@pytest.mark.parametrize("width", [1, 3])
def test_development_checkpoint_resume_never_preflights_inactive_test_tasks(setup, tmp_path, width):
    spec, task, store, memory, _, _ = setup
    inactive = task.model_copy(update={"id": "untouched-test", "family": "heldout-current-api", "split": "test", "base_sha": "4" * 40})
    spec = spec.model_copy(update={"tasks": [*spec.tasks, inactive], "phase_manifest_sha256": None, "execution_parallelism": width})
    initialized, models, pins = [], [], []

    class ReadyBackend(FakeBackend):
        def __init__(self, task, store, **kwargs):
            assert task.split == "dev", "Held-out tasks must remain untouched during development"
            super().__init__()
            self.task = task
            initialized.append(task.id)

        def pin_oracle(self):
            self.ready = True
            pins.append(self.task.id)
            return self.task

        def submit_patch(self, changes, *, timeout_seconds):
            assert self.ready, "Dispatch capabilities must be initialized on each fresh trial backend"
            return super().submit_patch(changes, timeout_seconds=timeout_seconds)

        def close(self):
            pass

    class Model:
        def __init__(self, settings):
            self.settings, self.count = settings, 0
            models.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def complete(self, messages, **kwargs):
            from buildtrace.experiments.openai import ModelTurn
            self.count += 1
            return ModelTurn(plan() if messages[-1].get("role") == "user" else patch(), 10, 5, "test-snapshot", {})

    output = tmp_path / "phase"
    kwargs = dict(split="dev", allow_publish=True, model_factory=Model, backend_factory=ReadyBackend,
                  memory_factory=lambda *args: memory, runtime_extra_files=())
    partial = run_experiment(spec, store, output, max_new_trials=1, **kwargs)
    assert len(partial) == 1
    complete = run_experiment(spec, store, output, **kwargs)
    assert len(complete) == 18 and all(record.success for record in complete)
    assert set(record.mode for record in complete) == set(AgentMode)
    assert sum(model.count for model in models) == 36
    assert "untouched-test" not in initialized
    assert len(pins) == len(initialized)
    assert load_spec(output / "experiment.json").phase_manifest_sha256
    assert len(json.loads((output / "packets.json").read_text(encoding="utf-8"))) == 2


def test_parallel_mode_block_has_isolated_conversations_and_preserves_all_infrastructure_outcomes(setup, tmp_path):
    from buildtrace.experiments.openai import ModelTurn, OpenAIError
    from buildtrace.experiments.metrics import read_records

    spec, _, store, memory, _, _ = setup
    spec = spec.model_copy(update={"execution_parallelism": 3, "phase_manifest_sha256": None})
    barrier, conversations = Barrier(3), []

    class ReadyBackend(FakeBackend):
        def __init__(self, task, store, **kwargs):
            super().__init__()
            self.task = task

        def pin_oracle(self):
            return self.task

        def close(self):
            pass

    class Model:
        def __init__(self, settings):
            self.settings = settings

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def complete(self, messages, **kwargs):
            initial = messages[-1].get("role") == "user"
            if initial:
                conversations.append(messages)
                barrier.wait(timeout=20)
                if "<memory_documents>" not in messages[1]["content"]:
                    raise OpenAIError("Synthetic transport failure")
            return ModelTurn(plan() if initial else patch(), 10, 5, "test-snapshot", {})

    output = tmp_path / "parallel"
    with pytest.raises(ValueError, match="entire active block was preserved"):
        run_experiment(spec, store, output, split="dev", allow_publish=True, model_factory=Model,
                       backend_factory=ReadyBackend, memory_factory=lambda *args: memory, runtime_extra_files=())
    records = read_records(output / "results.jsonl")
    assert len(records) == 3 and {record.mode for record in records} == set(AgentMode)
    assert sum(record.outcome == "infra-error" for record in records) == 1
    assert sum(record.success for record in records) == 2
    assert len({id(messages) for messages in conversations}) == 3


def test_v2_rejects_unbound_phase_before_any_model_calls(setup, tmp_path):
    spec, task, _, memory, phase, packet = setup
    model = ScriptedModel(spec.model, [])
    with pytest.raises(ValueError, match="different frozen phase"):
        run_trial(spec.model_copy(update={"phase_manifest_sha256": "9" * 64}), task, AgentMode.WIKITRACE,
                  0, FakeBackend(), model, tmp_path, memory=memory, packet=packet, phase_manifest=phase)
    assert not model.calls
