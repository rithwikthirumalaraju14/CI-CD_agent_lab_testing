import json
from collections import Counter

import pytest

from buildtrace import cli
from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.metrics import build_report, read_records
from buildtrace.experiments.models import AgentMode, ChangedHunk, fingerprint
from buildtrace.experiments.openai import ModelTurn
from buildtrace.experiments.workflow import apply_reviews, load_spec, review_template, run_experiment, schedule

from experiment_fixtures import experiment_spec, paired_records
from test_agent_modes import FakeBackend, FakeMemory, patch, plan


def test_counterbalanced_schedule_keeps_every_task_and_trial_paired():
    spec = experiment_spec()
    planned = schedule(spec, "test", include_reruns=True)
    assert len(planned) == 24
    for task in spec.tasks:
        positions = Counter()
        for trial in range(3):
            methods = [mode for current, mode, index, role in planned if current.id == task.id and index == trial and role == "reference"]
            assert set(methods) == set(AgentMode)
            positions.update((mode, position) for position, mode in enumerate(methods))
        assert set(positions.values()) == {1}


def test_full_synthetic_grid_report_cli_and_resume(tmp_path, capsys):
    spec = experiment_spec()
    models = []

    class AutoModel:
        def __init__(self, settings):
            self.settings, self.calls = settings, 0
            models.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def complete(self, messages, **kwargs):
            self.calls += 1
            message = plan() if messages[-1]["role"] == "user" else patch()
            return ModelTurn(message, 10, 5, "gpt-4.1-test-snapshot", {})

    class ReadyBackend(FakeBackend):
        def __init__(self, task, store, **kwargs):
            super().__init__()
            self.task = task

        def pin_oracle(self):
            self.task = self.task.model_copy(update={"oracle_sha": "1" * 40, "capture_sha256": "2" * 64})
            return self.task

        def close(self):
            pass

    output = tmp_path / "experiment"
    kwargs = dict(split="test", allow_publish=True, model_factory=AutoModel, backend_factory=ReadyBackend, memory_factory=lambda *args: FakeMemory())
    records = run_experiment(spec, EvidenceStore(tmp_path / "data"), output, **kwargs)
    assert len(records) == 18 and all(record.success for record in records)
    assert models[0].calls == 36
    assert len(read_records(output / "results.jsonl")) == 18
    frozen = load_spec(output / "experiment.json")
    report = build_report(frozen, records, resamples=20)
    assert all(method["gain"]["percent"] is None for name, method in report["methods"].items() if name != "no-memory")
    assert cli.main(["experiment", "report", "--spec", str(output / "experiment.json"), "--records", str(output / "results.jsonl"), "--output", str(tmp_path / "analysis"), "--bootstrap", "20"]) == 0
    assert "Synthetic data" in capsys.readouterr().out
    assert (tmp_path / "analysis/summary.md").exists()
    run_experiment(spec, EvidenceStore(tmp_path / "data"), output, **kwargs)
    assert models[-1].calls == 0
    with pytest.raises(ValueError, match="allow-publish"):
        run_experiment(spec, EvidenceStore(tmp_path), output, **(kwargs | {"allow_publish": False}))


def test_independent_review_is_a_derived_copy_bound_to_original_results(tmp_path):
    spec = experiment_spec()
    records = paired_records(spec)
    hunk = ChangedHunk(id="hunk-a", path="src/app.py", added_lines=1, deleted_lines=0, diff="+unrelated_refactor()\n")
    records[-1] = records[-1].model_copy(update={"hunks": [hunk]})
    original = "".join(record.model_dump_json() + "\n" for record in records).encode()
    form = review_template(records, original)
    assert "mode" not in form["hunks"][0]
    form["hunks"][0].update(classification="unnecessary", reviewer="independent-human")
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(form), encoding="utf-8")
    reviewed = apply_reviews(records, path, original)
    assert records[-1].hunks[0].classification == "unreviewed"
    assert reviewed[-1].hunks[0].classification == "unnecessary"
    with pytest.raises(ValueError, match="different results"):
        apply_reviews(records, path, original + b"\n")


def test_memory_location_is_not_experimental_identity():
    spec = experiment_spec()
    moved = spec.model_copy(update={"corpus": spec.corpus.parent / "moved-corpus"})
    assert fingerprint(spec) == fingerprint(moved)
