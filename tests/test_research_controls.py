"""Offline, adversarial checks for the operator-only research-v2 controls."""

import copy
import importlib.metadata
import json
import shutil
from collections import Counter
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from buildtrace.experiments import research
from buildtrace.experiments.models import Budget, Decision, ModelSettings, TaskSpec
from buildtrace.experiments.research import (
    ResearchControlError,
    content_sha256,
    create_input_manifest,
    create_review_packages,
    create_split_registry,
    evaluate_development_gate,
    freeze_evaluation,
    freeze_protocol,
    seed_schedule,
    verify_freeze,
    verify_input_manifest,
    verify_split_registry,
)


ROOT = Path(__file__).resolve().parents[1]
GATE_TASKS = ["dev-parser", "dev-resource"]
REVIEWERS = ["independent-reviewer"]
LOG = "raw/step-0000/initial-ci/logs/log-0001.txt"


def task(identifier, family, split, number):
    return TaskSpec(
        id=identifier, family=family, split=split, category="configuration",
        repository="example/project", base_sha=f"{number:040x}",
        initial_run_url=f"https://github.com/example/project/actions/runs/{1000 + number}/attempts/1",
        capture_id=f"{number:032x}", capture_sha256=f"{number:064x}",
        oracle_sha="f" * 40, expected_test_count=7,
    ).model_dump(mode="json")


def bytes_hash(content):
    return research.hashlib.sha256(content).hexdigest()


class Memory:
    def __init__(self, sources):
        self.contents = {}
        for index, source in enumerate(sources):
            base = f"raw/step-{index:04d}"
            log = b"client=old-api\nTraceback: timeout keyword is unavailable in this environment.\n"
            entries = []
            for kind, path, content in (
                ("log", "logs/log-0001.txt", log),
                ("source", "repository/pyproject.toml", b'[project]\ndependencies = ["client==1.2"]\n'),
                ("artifact", "artifacts/environment.json", b'{"client_version": "1.2", "python": "3.12"}\n'),
                ("artifact", "artifacts/verification-proof.json", b'{"verification_complete": true}\n'),
                ("log", "logs/blank.txt", b" \n\t\n"),
            ):
                self.contents[f"{base}/initial-ci/{path}"] = content
                entries.append({"id": f"entry-{len(entries):04d}", "kind": kind, "path": path, "sha256": bytes_hash(content)})
            capture = {"schema_version": 1, "id": source["capture_id"], "source_ref": source["base_sha"], "entries": entries}
            self.contents[f"{base}/initial-ci/manifest.json"] = json.dumps(capture).encode()
            self.contents[f"{base}/metadata.md"] = b"# Source metadata\n- session_success: 1\n"
            self.contents[f"{base}/verification.json"] = b'{"outcome": "solved", "oracle_passed": true}\n'
            self.contents[f"{base}/solver.full.jsonl"] = b'{"type": "message", "content": "My summary of the fix"}\n'
            self.contents[f"compilation/step-{index:04d}.md"] = b"A model-written interpretation of the old session.\n"
        self.manifest = {
            "schema_version": 1, "data_kind": "synthetic",
            "upstream": {"revision": "a" * 40, "repository": "https://example.com/compiler"},
            "compiler_model": ModelSettings(model="gpt-4.1").model_dump(mode="json"),
            "training_tasks": [
                {**{name: source[name] for name in ("id", "family", "repository", "base_sha")}, "source_run_id": f"training-run-{index}"}
                for index, source in enumerate(sources)
            ],
            "insights": [{"id": f"insight-{index}", "source_step": f"step-{index:04d}"} for index in range(len(sources))],
            "insights_sha256": "b" * 64,
            "files": {path: bytes_hash(content) for path, content in self.contents.items()},
        }
        self.sha256 = content_sha256(self.manifest)
        self.insights_sha256 = self.manifest["insights_sha256"]

    def read_source(self, relative):
        return self.contents[relative]


def source_checkout(destination):
    """Copy only the runtime files used by this test, never workspace data."""
    package = destination / "src/buildtrace"
    package.mkdir(parents=True)
    shutil.copytree(ROOT / "src/buildtrace/experiments", package / "experiments", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("__init__.py", "evidence.py", "github.py", "models.py"):
        shutil.copy2(ROOT / "src/buildtrace" / name, package / name)
    shutil.copy2(ROOT / "pyproject.toml", destination / "pyproject.toml")
    return destination


def verification(*, passed, attempt=1):
    return {
        "attempt": attempt, "candidate_sha": "c" * 40,
        "run_url": "https://github.com/example/project/actions/runs/9001/attempts/1",
        "oracle_run_url": "https://github.com/example/project/actions/runs/9002/attempts/1",
        "workflow_conclusion": "success" if passed else "failure",
        "required_checks_passed": passed, "protected_files_unchanged": True, "oracle_passed": passed,
        "capture_id": "d" * 32, "oracle_capture_id": "e" * 32, "elapsed_seconds": 1,
    }


def delivery(memory, document=LOG, *, layer="raw", insight="insight-0", offset=0, count=None):
    content = memory.contents[document]
    text = content.decode("utf-8", errors="replace")
    excerpt = text[offset:] if count is None else text[offset:offset + count]
    first = text[:offset].count("\n") + 1
    return {
        "id": "read-one", "sequence": 2, "insight_id": insight,
        "question": "Which API was actually observed?", "trigger": "environment-difference",
        "layer": layer, "document": "/work/corpus/" + document, "status": "delivered", "error": None,
        "offset": offset, "delivered_characters": len(excerpt), "document_sha256": bytes_hash(content),
        "excerpt_sha256": bytes_hash(excerpt.encode()), "first_line": first, "last_line": first + excerpt.count("\n"),
    }


def citation(read):
    return {
        **{name: read[name] for name in ("insight_id", "layer", "document", "document_sha256", "first_line", "last_line")},
        "source_run_id": "step-0000",
    }


def add_chain(run, memory, *, passed=False):
    before = Decision(action="apply", target="pyproject.toml", parameters={"version": "2"}, summary="Try the newer client").model_dump(mode="json")
    after = Decision(action="adapt", target="pyproject.toml", parameters={"version": "1.2"}, summary="Use the observed older API").model_dump(mode="json")
    read = delivery(memory, offset=len("client=old-api\n"))
    run["evidence_reads"] = [read]
    run["actions"] = [
        {"id": "before", "sequence": 1, "kind": "plan", "decision": before, "paths": [], "verification_attempt": None},
        {"id": read["id"], "sequence": read["sequence"], "kind": "history-read", "decision": before, "paths": [], "verification_attempt": None},
        {"id": "after", "sequence": 3, "kind": "plan", "decision": after, "paths": [], "verification_attempt": None},
        {"id": "patch", "sequence": 4, "kind": "patch", "decision": after, "paths": ["pyproject.toml"], "verification_attempt": 1},
    ]
    run["evidence_decisions"] = [{
        "id": "burst", "question": read["question"], "trigger": read["trigger"],
        "before": before, "after": after, "citations": [citation(read)], "review": "confirmed-change", "reviewer": REVIEWERS[0],
        "read_ids": [read["id"]], "before_action_id": "before", "after_action_id": "after",
        "contaminated": False, "material_effect": "change", "adoption": "adopted",
        "adoption_action_ids": ["patch"], "mechanism_reviewer": REVIEWERS[0],
    }]
    run["verifications"] = [verification(passed=passed)]
    run["outcome"] = "solved" if passed else "unsolved"
    run["hunks"] = [{"id": "wikitrace-hunk-secret-link", "path": "pyproject.toml", "diff": '-client==2\n+client==1.2\n', "added_lines": 1, "deleted_lines": 1}]


def grid(spec, manifest, memory, *, phase=None):
    entries = {entry["id"]: entry for entry in manifest["active"]["tasks"]}
    runs = []
    for cell in manifest["active"]["schedule"]:
        mode = cell["mode"]
        run = {
            **cell, "id": f"leaky-{cell['task_id']}-{mode}-{cell['trial']}", "schema_version": 2,
            "split": manifest["split"], "phase_manifest_sha256": phase or manifest["sha256"],
            "experiment_id": spec["id"], "experiment_sha256": "e" * 64,
            "task_sha256": entries[cell["task_id"]]["sha256"], "protocol_sha256": manifest["protocol_sha256"],
            "model": copy.deepcopy(spec["model"]), "budget": copy.deepcopy(spec["budget"]),
            "corpus_sha256": memory.sha256 if mode != "no-memory" else None,
            "insights_sha256": memory.insights_sha256 if mode != "no-memory" else None,
            "packet_sha256": "a" * 64 if mode != "no-memory" else None,
            "packet_insight_ids": ["insight-0", "insight-1"] if mode != "no-memory" else [],
            "outcome": "unsolved", "stop_reason": "budget", "returned_models": ["solver-snapshot"], "model_calls": 4,
            "actions": [], "evidence_reads": [], "evidence_decisions": [], "verifications": [], "hunks": [],
            "trajectory_path": "trials/local/trajectory.jsonl",
        }
        if manifest["split"] == "dev" and cell["task_id"] in GATE_TASKS and mode == "wikitrace" and cell["trial"] < 2:
            add_chain(run, memory, passed=cell["task_id"] == GATE_TASKS[0] and cell["trial"] == 0)
        runs.append(run)
    return runs


@pytest.fixture(scope="session")
def dependency_ownership():
    return importlib.metadata.packages_distributions()


@pytest.fixture
def study(tmp_path, monkeypatch, dependency_ownership):
    # Unit freeze tests isolate the dependency environment. A separate test below
    # checks effective dependency-source hashing, including editable modifications.
    monkeypatch.setattr(research, "_dependency_snapshot", lambda names: {"roots": sorted(names), "distributions": {"fixture": {"version": "1.2.3", "files_sha256": "9" * 64}}})
    monkeypatch.setattr(research.importlib.metadata, "packages_distributions", lambda: dependency_ownership)
    root = source_checkout(tmp_path / "checkout")
    sources = [task("train-api", "client-timeout-v1", "train", 1), task("train-contrast", "client-timeout-v2", "train", 2)]
    memory = Memory(sources)
    spec = {
        "schema_version": 2, "id": "research-development", "data_kind": "synthetic",
        "model": ModelSettings(model="gpt-4.1").model_dump(mode="json"), "budget": Budget().model_dump(mode="json"),
        "trials": 3, "seed": 42, "hold_out_families": True,
        "retrieval": {"selector": "deterministic-token-overlap-v2", "max_insights": 2, "max_characters": 4096},
        "tasks": [*sources, task("dev-parser", "quoted-record-boundaries", "dev", 3), task("dev-resource", "distribution-resource-path", "dev", 4), task("dev-inapplicable", "timezone-fold-handling", "dev", 5), task("heldout", "decimal-quantization", "test", 6)],
        "corpus": str(tmp_path / "machine-specific-corpus-location"), "corpus_sha256": memory.sha256,
        "insights_sha256": memory.insights_sha256, "split_registry": [], "phase_manifest_sha256": None,
    }
    manifest = create_input_manifest(spec, "dev", memory=memory, gate_task_ids=GATE_TASKS, source_root=root)
    return SimpleNamespace(spec=spec, memory=memory, manifest=manifest, records=grid(spec, manifest, memory), root=root)


def assess(study, *, reviewers=REVIEWERS, memory=True):
    return evaluate_development_gate(
        study.spec, study.records, manifest=study.manifest,
        memory=study.memory if memory else None,
        source_resolver=None if memory else study.memory.read_source,
        independent_reviewers=reviewers,
    )


def qualifying_runs(study):
    return [run for run in study.records if run["task_id"] == GATE_TASKS[0] and run["mode"] == "wikitrace" and run["trial"] < 2]


def replace_read(run, read):
    run["evidence_reads"][0] = read
    run["evidence_decisions"][0]["citations"] = [citation(read)]


def reseal(value):
    result = copy.deepcopy(value)
    result["sha256"] = content_sha256({name: item for name, item in result.items() if name != "sha256"})
    return result


def refreeze_sources(study):
    study.memory.manifest["files"] = {path: bytes_hash(content) for path, content in study.memory.contents.items()}
    study.memory.sha256 = content_sha256(study.memory.manifest)
    study.spec["corpus_sha256"] = study.memory.sha256
    study.manifest = create_input_manifest(study.spec, "dev", memory=study.memory, gate_task_ids=GATE_TASKS, source_root=study.root)
    study.records = grid(study.spec, study.manifest, study.memory)


@pytest.mark.parametrize("left,right", [("train", "dev"), ("train", "test"), ("dev", "test")])
@pytest.mark.parametrize("collision", ["id", "snapshot", "family"])
def test_cross_spec_registry_rejects_every_split_pair_and_identity_channel(left, right, collision):
    first = task("first", "specific-one", left, 1)
    second = task("second", "specific-two", right, 2)
    if collision == "id":
        second["id"] = first["id"]
    elif collision == "snapshot":
        second["base_sha"] = first["base_sha"]
        second["repository"] = "EXAMPLE/PROJECT"  # GitHub repository names are case-insensitive.
    else:
        second["family"] = first["family"].upper()
    registry = create_split_registry([first])
    with pytest.raises(ResearchControlError, match="split|leakage"):
        create_split_registry([second], registry)


def test_registry_is_content_addressed_order_independent_and_allows_same_split_reuse():
    first = task("first", "same-specific-family", "train", 1)
    second = {**first, "id": "another"}
    registry = create_split_registry([second, first, first])
    assert len(registry["identities"]) == 2
    assert create_split_registry([first], registry) == registry
    assert create_split_registry([first, second]) == registry
    assert verify_split_registry(registry) == registry["sha256"]
    changed = copy.deepcopy(registry)
    changed["identities"][0]["split"] = "test"
    with pytest.raises(ResearchControlError, match="Content address"):
        verify_split_registry(changed)
    with pytest.raises(ResearchControlError, match="Task ID"):
        create_split_registry([{**first, "base_sha": "f" * 40}], registry)


def test_candidate_hashes_only_active_task_capture_details_and_has_no_hash_cycle(study, tmp_path):
    original = copy.deepcopy(study.manifest)
    changed = copy.deepcopy(study.spec)
    changed["corpus"] = str(tmp_path / "a-different-machine/corpus")
    changed["phase_manifest_sha256"] = "9" * 64
    heldout = next(item for item in changed["tasks"] if item["split"] == "test")
    heldout.update(capture_sha256=None, oracle_sha=None, capture_id="not-preflighted")
    actual = create_input_manifest(changed, "dev", memory=study.memory, gate_task_ids=GATE_TASKS, source_root=study.root)
    assert actual == original
    assert [entry["id"] for entry in actual["active"]["tasks"]] == ["dev-inapplicable", "dev-parser", "dev-resource"]
    assert {entry["task_id"] for entry in actual["active"]["captures"]} == {"dev-inapplicable", *GATE_TASKS}
    assert str(tmp_path) not in json.dumps(actual)
    assert "phase_manifest_sha256" not in actual["protocol"]["controls"]


def test_candidate_supports_validated_models_and_root_relocation(study, tmp_path):
    class ValidatedJson(BaseModel):
        model_config = ConfigDict(extra="allow")
        schema_version: int

    relocated = tmp_path / "relocated"
    shutil.copytree(study.root, relocated)
    actual = create_input_manifest(ValidatedJson(**study.spec), "dev", memory=study.memory, gate_task_ids=GATE_TASKS, source_root=relocated)
    assert actual == study.manifest
    assert verify_input_manifest(actual, study.spec, memory=study.memory, source_root=relocated) == actual["sha256"]


@pytest.mark.parametrize("relative", ["src/buildtrace/github.py", "src/buildtrace/evidence.py", "src/buildtrace/models.py", "src/buildtrace/experiments/runner.py", "src/buildtrace/experiments/new_untracked_tool.py"])
def test_dirty_and_untracked_effective_runtime_source_invalidates_candidate(study, relative):
    path = study.root / relative
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(previous + "\n# A real on-disk source change, independent of Git.\n", encoding="utf-8")
    with pytest.raises(ResearchControlError, match="inputs changed"):
        verify_input_manifest(study.manifest, study.spec, memory=study.memory, source_root=study.root)


def test_explicit_external_verifier_sources_are_portable_and_reverified(study, tmp_path):
    first, second = tmp_path / "verifier-a.py", tmp_path / "verifier-b.py"
    first.write_text("EXPECTED = 7\n", encoding="utf-8")
    second.write_bytes(first.read_bytes())
    kwargs = dict(memory=study.memory, gate_task_ids=GATE_TASKS, source_root=study.root)
    left = create_input_manifest(study.spec, "dev", extra_files={"infra/verifier.py": first}, **kwargs)
    right = create_input_manifest(study.spec, "dev", extra_files={"infra/verifier.py": second}, **kwargs)
    assert left == right
    second.write_text("EXPECTED = 6\n", encoding="utf-8")
    with pytest.raises(ResearchControlError, match="inputs changed"):
        verify_input_manifest(left, study.spec, memory=study.memory, extra_files={"infra/verifier.py": second}, source_root=study.root)


def test_environment_files_and_absolute_task_paths_are_rejected_before_read(study, monkeypatch):
    original_read = Path.read_bytes

    def no_environment_reads(path):
        assert not any(part.startswith(".env") for part in path.parts), "environment file was accessed"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", no_environment_reads)
    with pytest.raises(ResearchControlError, match="Environment files"):
        create_input_manifest(study.spec, "dev", memory=study.memory, extra_files={"harmless.py": study.root / ".env"}, source_root=study.root)
    broken = copy.deepcopy(study.spec)
    next(item for item in broken["tasks"] if item["id"] == GATE_TASKS[0])["editable_paths"] = ["C:/machine/source.py"]
    with pytest.raises(ResearchControlError, match="absolute paths"):
        create_input_manifest(broken, "dev", memory=study.memory, source_root=study.root)


def test_dependency_freeze_pins_version_revision_and_modified_effective_source(tmp_path, monkeypatch):
    package = tmp_path / "effective_dependency"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text("BEHAVIOR = 'original'\n", encoding="utf-8")
    direct = {"url": "https://synthetic-credential@example.com/dependency.git?key=synthetic-credential", "vcs_info": {"vcs": "git", "commit_id": "a" * 40}}

    class Distribution:
        files = [PurePosixPath("effective_dependency/__init__.py")]
        requires = []
        version = "1.2.3"
        metadata = {"Name": "effective-dependency"}

        def read_text(self, name):
            return "effective_dependency\n" if name == "top_level.txt" else json.dumps(direct)

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(research.importlib.util, "find_spec", lambda name: SimpleNamespace(submodule_search_locations=[str(package)], origin=str(source)))
    first = research._dependency_snapshot(["effective-dependency"])
    pin = first["distributions"]["effective-dependency"]
    assert pin["version"] == "1.2.3" and pin["provenance"]["revision"] == "a" * 40
    assert pin["provenance"]["repository"] == "https://example.com/dependency.git"
    assert str(tmp_path) not in json.dumps(first) and "synthetic-credential" not in json.dumps(first)
    source.write_text("BEHAVIOR = 'locally modified'\n", encoding="utf-8")
    second = research._dependency_snapshot(["effective-dependency"])
    assert second["distributions"]["effective-dependency"]["files_sha256"] != pin["files_sha256"]
    assert second["distributions"]["effective-dependency"]["version"] == pin["version"]


def test_seed_schedule_is_complete_counterbalanced_and_v2_dev_has_all_modes(study):
    cells = seed_schedule(study.spec, "dev")
    assert cells == seed_schedule(study.spec, "dev")
    assert len(cells) == 27
    for identifier in [*GATE_TASKS, "dev-inapplicable"]:
        positions = Counter()
        for trial in range(3):
            modes = [cell["mode"] for cell in cells if cell["task_id"] == identifier and cell["trial"] == trial]
            assert set(modes) == set(research.MODES)
            positions.update((mode, index) for index, mode in enumerate(modes))
        assert set(positions.values()) == {1}
    assert {cell["mode"] for cell in seed_schedule(study.spec, "train")} == {"no-memory"}


def test_gate_passes_with_replay_and_adoption_without_superiority_or_reading_every_task(study):
    for run in study.records:
        if run["mode"] != "wikitrace":
            run.update(outcome="solved", verifications=[verification(passed=True)])
    original = copy.deepcopy(study.records)
    report = assess(study)
    assert report["passed"], report["failures"]
    assert report["expected_cells"] == report["observed_records"] == 27
    assert report["verified_repair_chains"] == 1
    assert report["tasks"]["dev-inapplicable"]["qualifying_trials"] == []
    assert report["tasks"][GATE_TASKS[0]]["qualifying_trials"] == [0, 1]
    assert report["outcome_counts"]["solved"] == 19
    assert assess(study, memory=False) == report  # The corpus-relative resolver API is sufficient.
    assert study.records == original


def test_reviewed_refinement_and_subsequent_observation_can_qualify(study):
    run = qualifying_runs(study)[1]
    run["evidence_decisions"][0]["material_effect"] = "refinement"
    run["actions"][-1].update(kind="current-observation", verification_attempt=None)
    run["verifications"] = []
    assert assess(study)["passed"]


@pytest.mark.parametrize("path", ["repository/pyproject.toml", "artifacts/environment.json"])
def test_original_source_and_environment_observations_can_qualify(study, path):
    for run in qualifying_runs(study):
        replace_read(run, delivery(study.memory, "raw/step-0000/initial-ci/" + path))
    assert assess(study)["passed"]


def test_compilation_navigation_can_precede_a_replayed_original_read(study):
    for run in qualifying_runs(study):
        event = run["evidence_decisions"][0]
        raw = run["evidence_reads"][0]
        raw["sequence"] = 3
        compilation = delivery(study.memory, "compilation/step-0000.md", layer="compilation")
        compilation["id"] = "navigation"
        run["evidence_reads"] = [compilation, raw]
        run["actions"][1]["sequence"] = 3
        run["actions"][2]["sequence"] = 4
        run["actions"][3]["sequence"] = 5
        run["actions"].insert(1, {"id": "navigation", "sequence": 2, "kind": "history-read", "decision": event["before"], "paths": [], "verification_attempt": None})
        event["read_ids"] = ["navigation", raw["id"]]
        event["citations"] = [citation(compilation), citation(raw)]
    assert assess(study)["passed"]


@pytest.mark.parametrize("document,layer", [
    ("raw/step-0000/metadata.md", "raw"),
    ("raw/step-0000/verification.json", "raw"),
    ("raw/step-0000/solver.full.jsonl", "raw"),
    ("raw/step-0000/initial-ci/manifest.json", "raw"),
    ("raw/step-0000/initial-ci/artifacts/verification-proof.json", "raw"),
    ("compilation/step-0000.md", "compilation"),
    ("raw/step-0000/initial-ci/logs/blank.txt", "raw"),
])
def test_metadata_verification_compilation_and_blank_deliveries_never_satisfy_gate(study, document, layer):
    for run in qualifying_runs(study):
        replace_read(run, delivery(study.memory, document, layer=layer))
    report = assess(study)
    assert not report["passed"]
    assert report["tasks"][GATE_TASKS[0]]["qualifying_trials"] == []


@pytest.mark.parametrize("field,value", [
    ("status", "error"), ("error", "request failed"), ("delivered_characters", 0),
    ("delivered_characters", 100000), ("offset", 100000), ("offset", True),
    ("document_sha256", "0" * 64), ("excerpt_sha256", "0" * 64),
    ("first_line", 99), ("last_line", 99), ("first_line", True),
    ("insight_id", "unlinked-insight"), ("document", "/work/corpus/raw/step-0001/initial-ci/logs/log-0001.txt"),
])
def test_delivery_flags_hashes_ranges_and_links_must_actually_replay(study, field, value):
    for run in qualifying_runs(study):
        run["evidence_reads"][0][field] = value
    assert not assess(study)["passed"]


def test_changed_source_bytes_and_capture_entry_mismatches_are_not_trusted(study):
    study.memory.contents[LOG] += b"Changed after freezing.\n"
    report = assess(study)
    assert not report["passed"]
    assert any("checksum mismatch" in item["reason"] for item in report["tasks"][GATE_TASKS[0]]["rejected_chains"])


@pytest.mark.parametrize("failure", ["entry-sha", "source-sha"])
def test_frozen_file_hash_alone_does_not_establish_original_capture_linkage(study, failure):
    path = "raw/step-0000/initial-ci/manifest.json"
    capture = json.loads(study.memory.contents[path])
    if failure == "entry-sha":
        capture["entries"][0]["sha256"] = "0" * 64
    else:
        capture["source_ref"] = "0" * 40
    study.memory.contents[path] = json.dumps(capture).encode()
    refreeze_sources(study)
    assert not assess(study)["passed"]


@pytest.mark.parametrize("name,payload,delivered,expected", [
    ("read_current_log", "Traceback: original observed failure", "payload", True),
    ("read_current_file", "client = 'café'\n", "payload", True),
    ("read_current_file", "client = 'café'\n", "header-only", False),
    ("read_current_log", " \n\t", "payload", False),
    ("submit_patch", "verification was successful", "payload", False),
    ("read_current_log", "request failed", "error", False),
])
def test_raw_journal_reads_must_deliver_actual_successful_tool_observation_payloads(study, name, payload, delivered, expected):
    path = "raw/step-0000/solver.full.jsonl"
    field = "content" if name == "read_current_file" else "text"
    row = {"type": "function_call", "name": name, "arguments": {"path": "pyproject.toml"}, "is_error": delivered == "error", "result": {field: payload}}
    text = json.dumps(row, ensure_ascii=False) + "\n"
    study.memory.contents[path] = text.encode()
    refreeze_sources(study)
    # The successful cases deliver just part of a payload, not the entire row.
    if delivered == "header-only":
        offset, count = 0, text.index('"result"')
    else:
        offset = text.index(json.dumps(payload, ensure_ascii=False)) + 1
        count = len(json.dumps(payload, ensure_ascii=False)) - 2
    for run in qualifying_runs(study):
        replace_read(run, delivery(study.memory, path, offset=offset, count=count))
    report = assess(study)
    assert report["passed"] is expected, report["failures"]


def test_source_resolver_errors_cannot_be_relabelled_as_deliveries(study):
    def unavailable(relative):
        raise RuntimeError("an unavailable original source")

    report = evaluate_development_gate(study.spec, study.records, manifest=study.manifest, source_resolver=unavailable, independent_reviewers=REVIEWERS)
    assert not report["passed"]
    assert any("could not replay" in item["reason"] for item in report["tasks"][GATE_TASKS[0]]["rejected_chains"])


def test_rejected_third_repeat_is_preserved_without_erasing_two_clean_repeats(study):
    third = next(run for run in study.records if run["task_id"] == GATE_TASKS[0] and run["mode"] == "wikitrace" and run["trial"] == 2)
    add_chain(third, study.memory)
    third["evidence_reads"][0].update(status="error", error="request failed", delivered_characters=0)
    report = assess(study)
    assert report["passed"]
    assert report["tasks"][GATE_TASKS[0]]["rejected_chains"]
    assert report["observed_records"] == 27


@pytest.mark.parametrize("mutation", ["unreviewed", "no-adoption", "no-reviewer", "contaminated", "wording-only", "missing-action", "unrelated-action", "wrong-snapshot", "no-paths", "wrong-attempt", "intervening-observation", "intervening-plan", "missing-read-action", "duplicate-sequence", "citation-mismatch"])
def test_adversarial_decision_and_action_metadata_cannot_manufacture_a_chain(study, mutation):
    for run in qualifying_runs(study):
        event = run["evidence_decisions"][0]
        if mutation == "unreviewed":
            event["material_effect"] = "unreviewed"
        elif mutation == "no-adoption":
            event["adoption"] = "not-adopted"
        elif mutation == "no-reviewer":
            event["mechanism_reviewer"] = None
        elif mutation == "contaminated":
            event["contaminated"] = True
        elif mutation == "wording-only":
            after = {**event["before"], "summary": "The same plan in different words"}
            event["after"] = after
            event["material_effect"] = "refinement"
            run["actions"][2]["decision"] = run["actions"][3]["decision"] = after
        elif mutation == "missing-action":
            event["adoption_action_ids"] = ["invented-patch"]
        elif mutation == "unrelated-action":
            run["actions"][-1]["paths"] = ["src/unrelated.py"]
        elif mutation == "wrong-snapshot":
            run["actions"][-1]["decision"] = event["before"]
        elif mutation == "no-paths":
            run["actions"][-1]["paths"] = []
        elif mutation == "wrong-attempt":
            run["actions"][-1]["verification_attempt"] = 2
        elif mutation == "intervening-observation":
            run["actions"][2]["sequence"] = 4
            run["actions"][3]["sequence"] = 5
            run["actions"].insert(2, {"id": "new-current-fact", "sequence": 3, "kind": "current-observation", "decision": None, "paths": ["src/app.py"], "verification_attempt": None})
        elif mutation == "intervening-plan":
            run["actions"][-1]["sequence"] = 5
            run["actions"].insert(3, {"id": "later-plan", "sequence": 4, "kind": "plan", "decision": event["before"], "paths": [], "verification_attempt": None})
        elif mutation == "missing-read-action":
            del run["actions"][1]
        elif mutation == "duplicate-sequence":
            run["actions"][-1]["sequence"] = run["actions"][2]["sequence"]
        elif mutation == "citation-mismatch":
            event["citations"][0]["first_line"] = 1
    report = assess(study)
    assert not report["passed"]
    assert report["tasks"][GATE_TASKS[0]]["qualifying_trials"] == []


def test_independent_review_requires_an_allowlist_and_excludes_solver_identities(study):
    assert not assess(study, reviewers=[])["passed"]
    for run in qualifying_runs(study):
        run["evidence_decisions"][0]["mechanism_reviewer"] = "solver-snapshot"
    assert not assess(study, reviewers=[*REVIEWERS, "solver-snapshot"])["passed"]


@pytest.mark.parametrize("mutation", ["missing-cell", "duplicate-cell", "wrong-phase", "packet-mismatch", "wrong-split", "foreign-task", "forbidden-source-access"])
def test_gate_requires_the_whole_matched_grid_not_just_favorable_wikitrace_runs(study, mutation):
    if mutation == "missing-cell":
        study.records.pop()
    elif mutation == "duplicate-cell":
        study.records.append(copy.deepcopy(study.records[0]))
    elif mutation == "wrong-phase":
        study.records[0]["phase_manifest_sha256"] = "f" * 64
    elif mutation == "packet-mismatch":
        next(run for run in study.records if run["mode"] == "insights-only")["packet_sha256"] = "f" * 64
    elif mutation == "wrong-split":
        study.records[0]["split"] = "test"
    elif mutation == "foreign-task":
        study.records[0]["task_id"] = "not-declared"
    else:
        next(run for run in study.records if run["mode"] == "insights-only")["evidence_reads"] = [delivery(study.memory)]
    assert not assess(study)["passed"]


def test_two_qualifying_events_in_one_repeat_do_not_substitute_for_two_repeats(study):
    runs = qualifying_runs(study)
    runs[1]["evidence_decisions"] = []
    event = copy.deepcopy(runs[0]["evidence_decisions"][0])
    event["id"] = "duplicated-attribution"
    runs[0]["evidence_decisions"].append(event)
    assert not assess(study)["passed"]


def test_a_verified_repair_must_be_at_the_end_of_a_qualifying_adopted_patch_chain(study):
    for run in study.records:
        if run["verifications"]:
            run.update(outcome="unsolved", verifications=[verification(passed=False)])
    report = assess(study)
    assert all(len(report["tasks"][identifier]["qualifying_trials"]) == 2 for identifier in GATE_TASKS)
    assert not report["passed"] and report["verified_repair_chains"] == 0


@pytest.mark.parametrize("attack", ["duplicate-attempt", "unverified-later-patch"])
def test_a_later_observation_cannot_hide_an_unadopted_last_patch(study, attack):
    run = next(run for run in qualifying_runs(study) if run["outcome"] == "solved")
    event = run["evidence_decisions"][0]
    attempt = None
    if attack == "duplicate-attempt":
        run["verifications"] = [verification(passed=False), verification(passed=True, attempt=2)]
        run["actions"][-1]["verification_attempt"] = 2
        attempt = 2
    run["actions"].extend([
        {"id": "unadopted-patch", "sequence": 5, "kind": "patch", "decision": event["before"], "paths": ["src/unrelated.py"], "verification_attempt": attempt},
        {"id": "later-observation", "sequence": 6, "kind": "current-observation", "decision": event["after"], "paths": ["pyproject.toml"], "verification_attempt": None},
    ])
    event["adoption_action_ids"] = ["patch", "later-observation"]
    report = assess(study)
    assert not report["passed"]
    assert report["verified_repair_chains"] == 0


@pytest.mark.parametrize("trial", [None, True])
def test_malformed_json_repeat_fields_are_rejected_explicitly(study, trial):
    study.records[0]["trial"] = trial
    with pytest.raises(ResearchControlError, match="trial must be an integer"):
        assess(study)


def test_gate_task_selection_is_predeclared_and_must_span_specific_families(study):
    # Two tasks may legitimately share a family within dev, but cannot establish
    # this gate's different-family coverage.
    for item in study.spec["tasks"]:
        if item["id"] in GATE_TASKS:
            item["family"] = "same-specific-family"
    study.manifest = create_input_manifest(study.spec, "dev", memory=study.memory, gate_task_ids=GATE_TASKS, source_root=study.root)
    study.records = grid(study.spec, study.manifest, study.memory)
    assert not assess(study)["passed"]
    changed = copy.deepcopy(study.manifest)
    changed["active"]["gate_task_ids"] = ["dev-inapplicable"]
    with pytest.raises(ResearchControlError, match="Content address"):
        evaluate_development_gate(study.spec, study.records, manifest=changed, memory=study.memory, independent_reviewers=REVIEWERS)


def approve(study):
    return freeze_protocol(study.spec, study.records, manifest=study.manifest, memory=study.memory, independent_reviewers=REVIEWERS, source_root=study.root)


def test_protocol_and_heldout_freezes_are_distinct_noncyclic_and_bind_resume_records(study):
    approved = approve(study)
    assert approved["sha256"] != study.manifest["sha256"]
    assert verify_freeze(approved, study.manifest, records=study.records, spec=study.spec) == approved["sha256"]
    heldout_spec = {**copy.deepcopy(study.spec), "id": "research-heldout"}
    candidate = create_input_manifest(heldout_spec, "test", memory=study.memory, source_root=study.root)
    frozen = freeze_evaluation(heldout_spec, manifest=candidate, protocol_freeze=approved, memory=study.memory, source_root=study.root)
    assert len({study.manifest["sha256"], approved["sha256"], candidate["sha256"], frozen["sha256"]}) == 4
    heldout_spec["phase_manifest_sha256"] = frozen["sha256"]
    assert create_input_manifest(heldout_spec, "test", memory=study.memory, source_root=study.root) == candidate
    records = grid(heldout_spec, candidate, study.memory, phase=frozen["sha256"])
    assert verify_freeze(frozen, candidate, protocol_freeze=approved, records=records[:2], spec=heldout_spec) == frozen["sha256"]
    records[0]["phase_manifest_sha256"] = candidate["sha256"]
    with pytest.raises(ResearchControlError, match="phase manifest"):
        verify_freeze(frozen, candidate, protocol_freeze=approved, records=records)
    wrong = reseal({**frozen, "protocol_freeze_sha256": "0" * 64})
    with pytest.raises(ResearchControlError, match="identities"):
        verify_freeze(wrong, candidate, protocol_freeze=approved)


def test_failed_gate_and_changed_source_cannot_be_frozen(study):
    study.records = [run for run in study.records if run["mode"] == "wikitrace"]
    with pytest.raises(ResearchControlError, match="Development gate failed"):
        approve(study)
    study.records = grid(study.spec, study.manifest, study.memory)
    path = study.root / "src/buildtrace/experiments/runner.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# protocol drift\n", encoding="utf-8")
    with pytest.raises(ResearchControlError, match="inputs changed"):
        approve(study)


@pytest.mark.parametrize("control", ["budget", "retrieval", "seed"])
def test_heldout_freeze_rejects_post_development_protocol_tuning(study, control):
    approved = approve(study)
    heldout_spec = {**copy.deepcopy(study.spec), "id": "new-test"}
    if control == "budget":
        heldout_spec["budget"]["total_tokens"] += 1000
    elif control == "retrieval":
        heldout_spec["retrieval"]["max_insights"] += 1
    else:
        heldout_spec["seed"] += 1
    candidate = create_input_manifest(heldout_spec, "test", memory=study.memory, source_root=study.root)
    with pytest.raises(ResearchControlError, match="development-approved"):
        freeze_evaluation(heldout_spec, manifest=candidate, protocol_freeze=approved, memory=study.memory, source_root=study.root)


def test_resealing_a_modified_task_or_attaching_changed_dev_reviews_breaks_identity(study):
    broken = copy.deepcopy(study.manifest)
    broken["active"]["tasks"][0]["task"]["expected_test_count"] = 1
    broken["active"]["task_manifest_sha256"] = content_sha256(broken["active"]["tasks"])
    with pytest.raises(ResearchControlError, match="task identity"):
        verify_input_manifest(reseal(broken), study.spec, memory=study.memory, source_root=study.root)
    approved = approve(study)
    qualifying_runs(study)[0]["evidence_decisions"][0]["adoption"] = "not-adopted"
    with pytest.raises(ResearchControlError, match="reviews|review/records"):
        verify_freeze(approved, study.manifest, records=study.records)


def test_review_packages_remove_linkable_hunk_metadata_and_return_private_mapping_separately(study):
    original = copy.deepcopy(study.records)
    raw = "".join(json.dumps(run) + "\n" for run in study.records).encode()
    hunks, evidence, private = create_review_packages(study.records, raw, task_context={identifier: "Preserve the required client behavior." for identifier in GATE_TASKS})
    public_text = json.dumps(hunks)
    assert hunks["items"] and evidence["items"]
    for run in study.records:
        assert run["id"] not in public_text
    assert "wikitrace" not in public_text
    assert "hunk-secret-link" not in public_text
    assert "run_id" not in public_text and "task_id" not in public_text and "source_results_sha256" not in public_text
    hunk_ids = {item["item_id"] for item in hunks["items"]}
    evidence_ids = {item["item_id"] for item in evidence["items"]}
    assert not hunk_ids & evidence_ids
    assert hunk_ids == set(private["hunks"]) and evidence_ids == set(private["evidence"])
    assert private["source_results_sha256"] == bytes_hash(raw)
    assert private["hunk_package_sha256"] == hunks["sha256"]
    assert private["evidence_package_sha256"] == evidence["sha256"]
    another = create_review_packages(study.records, raw)
    assert not hunk_ids & {item["item_id"] for item in another[0]["items"]}
    evidence["items"][0]["actions"].clear()
    assert study.records == original


def test_review_export_rejects_a_mapping_to_different_original_result_bytes(study):
    raw = "".join(json.dumps(run) + "\n" for run in study.records).encode()
    study.records[0]["stop_reason"] = "changed after original export"
    with pytest.raises(ResearchControlError, match="original results"):
        create_review_packages(study.records, raw)


def test_evidence_review_package_can_include_exact_replayed_excerpts_separately(study):
    raw = "".join(json.dumps(run) + "\n" for run in study.records).encode()
    hunks, evidence, private = create_review_packages(study.records, raw, manifest=study.manifest, memory=study.memory)
    read = evidence["items"][0]["reads"][0]
    expected = study.memory.contents[LOG].decode()[read["offset"]:read["offset"] + read["delivered_characters"]]
    assert read["replay"]["excerpt"] == expected
    assert read["replay"]["excerpt_sha256"] == bytes_hash(expected.encode())
    assert read["replay"]["original_observation"] is True
    assert expected not in json.dumps(hunks)
    assert private["evidence_package_sha256"] == evidence["sha256"]
