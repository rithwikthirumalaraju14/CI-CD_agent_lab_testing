import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("research_v2_verify", ROOT / "infra/research_v2/verify.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


@pytest.fixture
def verifier_args(tmp_path, monkeypatch):
    candidate, trusted, artifacts = (tmp_path / name for name in ("candidate", "trusted", "evidence"))
    candidate.mkdir()
    trusted.mkdir()
    held = trusted / "oracle_tests"
    held.mkdir()
    (held / "test_original.py").write_text("def test_first():\n    assert True\n\ndef test_second():\n    assert True\n", encoding="utf-8")
    contract = trusted / "contract.json"
    contract.write_text(json.dumps({"schema_version": 1, "expected_test_count": 2, "tests_path": "oracle_tests"}), encoding="utf-8")
    args = SimpleNamespace(
        candidate=candidate, trusted=trusted, artifacts=artifacts, contract=contract,
        oracle_mode="true", source_sha="e" * 40, workflow_sha="a" * 40,
        run_id=102, run_attempt=1,
    )
    monkeypatch.setattr(VERIFY, "_git_sha", lambda path: args.source_sha if path == candidate else args.workflow_sha)
    return args


def _report(args, *, count=2, child=""):
    cases = "".join(f'<testcase name="original_{number}">{child if number == 0 else ""}</testcase>' for number in range(count))
    (args.artifacts / "pytest.xml").write_text(f'<testsuite tests="{count}" failures="0" errors="0" skipped="0">{cases}</testsuite>', encoding="utf-8")


def test_separately_held_oracle_ignores_candidate_discovery_conftests_and_plugins(verifier_args, monkeypatch):
    args = verifier_args
    (args.candidate / "conftest.py").write_text('raise RuntimeError("candidate conftest must not load")\n', encoding="utf-8")
    (args.candidate / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "--ignore=."\npython_files = "never_collect_*.py"\n', encoding="utf-8")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--deselect=test_original.py")
    monkeypatch.setenv("PYTEST_PLUGINS", "nonexistent_candidate_plugin")
    monkeypatch.setenv("PYTHONPATH", str(args.candidate))
    assert VERIFY.record(args) == 0
    # This is a real, offline pytest subprocess in .venv, against the held suite.
    assert VERIFY.run_tests(args) == 0
    assert VERIFY.finalize(args) == 0
    proof = json.loads((args.artifacts / "verification-proof.json").read_text(encoding="utf-8"))
    assert proof["verification_complete"] and proof["protected_tests_unchanged"]
    assert proof["observed_test_count"] == proof["expected_test_count"] == 2
    assert proof["checked_out_sha"] == args.source_sha and proof["workflow_sha"] == args.workflow_sha
    assert not (args.candidate / "oracle_tests").exists()


@pytest.mark.parametrize("failure", ["missing-case", "hidden-skip", "mutated-tests", "nonzero-exit"])
def test_protected_oracle_failures_emit_diagnostics_without_success(verifier_args, monkeypatch, failure):
    args = verifier_args
    assert VERIFY.record(args) == 0

    def pytest_run(command, **kwargs):
        _report(args, count=1 if failure == "missing-case" else 2, child="<skipped/>" if failure == "hidden-skip" else "")
        if failure == "mutated-tests":
            (args.trusted / "oracle_tests/test_original.py").write_text("def test_replacement():\n    assert True\n", encoding="utf-8")
        return SimpleNamespace(returncode=1 if failure == "nonzero-exit" else 0)

    monkeypatch.setattr(VERIFY.subprocess, "run", pytest_run)
    assert VERIFY.run_tests(args) == 1
    assert VERIFY.finalize(args) == 1
    proof = json.loads((args.artifacts / "verification-proof.json").read_text(encoding="utf-8"))
    assert proof["verification_complete"] is False
    assert proof["protected_tests_unchanged"] == (failure != "mutated-tests")
    assert (args.artifacts / "test-execution.json").exists()


def test_changed_trusted_count_or_missing_test_execution_cannot_finalize_green(verifier_args):
    args = verifier_args
    VERIFY.record(args)
    _report(args)
    assert VERIFY.finalize(args) == 1
    (args.artifacts / "test-execution.json").write_text('{"returncode": 0}', encoding="utf-8")
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    contract["expected_test_count"] = 1
    args.contract.write_text(json.dumps(contract), encoding="utf-8")
    _report(args, count=1)
    assert VERIFY.finalize(args) == 1
    proof = json.loads((args.artifacts / "verification-proof.json").read_text(encoding="utf-8"))
    assert not proof["protected_tests_unchanged"] and not proof["verification_complete"]


def test_unconfigured_count_and_candidate_owned_contract_fail_closed(verifier_args):
    args = verifier_args
    args.contract.write_text('{"schema_version": 1, "expected_test_count": null, "tests_path": "oracle_tests"}', encoding="utf-8")
    with pytest.raises(ValueError, match="positive original expected_test_count"):
        VERIFY.record(args)
    args.contract = args.candidate / "contract.json"
    args.contract.write_text('{"schema_version": 1, "expected_test_count": 1, "tests_path": "tests"}', encoding="utf-8")
    with pytest.raises(ValueError, match="trusted checkout"):
        VERIFY.record(args)
    assert not (args.artifacts / "environment-before-install.json").exists()


def test_generic_workflow_uses_correlated_names_two_pinned_checkouts_and_universal_cli():
    document = yaml.load((ROOT / "infra/research_v2/trusted-ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert {"source_sha", "oracle_mode", "dispatch_id"} <= document["on"]["workflow_dispatch"]["inputs"].keys()
    assert "BuildTrace {0} {1} {2}" in document["run-name"]
    job = document["jobs"]["bench-checks"]
    checkouts = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout")]
    assert checkouts[0]["with"]["ref"] == "${{ github.sha }}"
    assert checkouts[1]["with"]["ref"] == "${{ inputs.source_sha || github.sha }}"
    assert all(step["with"]["persist-credentials"] == "false" for step in checkouts)
    steps = {step["name"]: step for step in job["steps"]}
    assert '".[dev]" --constraint ci/constraints.txt' in steps["Install project and test dependencies"]["run"]
    assert "bench_app" in steps["Run example"]["run"] and "examples/input.json" in steps["Run example"]["run"]
    assert steps["Upload execution evidence"]["with"]["name"] == "bench-ci-${{ github.run_id }}-${{ github.run_attempt }}"
    assert "_trusted" not in steps["Upload execution evidence"]["with"]["path"]
