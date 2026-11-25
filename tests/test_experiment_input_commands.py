import json
from types import SimpleNamespace

from buildtrace import cli
from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.models import ExperimentSpec, ModelSettings, TaskSpec
from buildtrace.experiments.workflow import load_spec

from conftest import RUN_URL, SHA


def training_spec(capture_id="0" * 32):
    return ExperimentSpec(
        id="input-command-test", data_kind="synthetic", model=ModelSettings(model="gpt-4.1"),
        tasks=[TaskSpec(id="train-1", family="train-family", split="train", category="import", repository="example/invoice", base_sha=SHA, initial_run_url=RUN_URL + "/attempts/2", capture_id=capture_id)],
    )


def test_capture_and_attach_commands_make_no_model_calls(tmp_path, github_simulator, monkeypatch):
    from buildtrace.experiments import commands

    make_factory, _ = github_simulator
    monkeypatch.setattr(commands, "GitHubActionsClient", make_factory())
    monkeypatch.setattr(commands, "OpenAIClient", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Input preparation must not call OpenAI")))
    source, archived = tmp_path / "source.json", tmp_path / "archived.json"
    source.write_text(training_spec().model_dump_json(), encoding="utf-8")
    assert cli.main(["experiment", "capture-inputs", "--spec", str(source), "--output", str(archived), "--data-dir", str(tmp_path / "data")]) == 0
    prepared = load_spec(archived)
    assert prepared.tasks[0].capture_id != "0" * 32
    assert prepared.tasks[0].capture_sha256 is not None
    assert EvidenceStore(tmp_path / "data").verify(prepared.tasks[0].capture_id)["integrity"] == "verified"
    memory = SimpleNamespace(directory=tmp_path / "corpus", sha256="a" * 64, insights_sha256="b" * 64, assert_test_separation=lambda spec: None)
    monkeypatch.setattr(commands, "FrozenMemory", lambda path: memory)
    attached = tmp_path / "ready.json"
    assert cli.main(["experiment", "attach-memory", "--spec", str(archived), "--corpus", str(memory.directory), "--output", str(attached)]) == 0
    assert load_spec(attached).corpus_sha256 == "a" * 64


def test_local_validation_reports_missing_key_without_a_paid_call(tmp_path, collected_run, monkeypatch, capsys):
    from buildtrace.experiments import workflow

    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(workflow, "resolve_github_token", lambda: "test")
    store = EvidenceStore(tmp_path / "data")
    capture = store.save(collected_run)
    spec_file = tmp_path / "training.json"
    spec_file.write_text(training_spec(capture["id"]).model_dump_json(), encoding="utf-8")
    assert cli.main(["experiment", "validate", "--spec", str(spec_file), "--data-dir", str(tmp_path / "data")]) == 2
    result = json.loads(capsys.readouterr().out)
    assert not result["local_prerequisites_ready"]
    assert any("OPENAI_API_KEY" in item for item in result["remaining_inputs"])
