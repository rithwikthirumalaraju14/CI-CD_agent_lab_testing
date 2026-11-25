from collections import Counter, defaultdict
from dataclasses import replace
import ast
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

import pytest

from buildtrace.research_benchmark import (
    AnchorError,
    CASES,
    catalogue,
    load_contract,
    materialize_baseline,
    materialize_case,
    reference_edits,
    snapshot_files,
    verify_snapshot,
)
from buildtrace.research_benchmark.catalogue import (
    HARD_DEVELOPMENT_CASES, HARD_TEST_CASES, get_case, get_seed,
)
from buildtrace.research_benchmark.materialize import sha256, tree_sha256
from buildtrace.research_benchmark.model import Edit, VerificationContract
from buildtrace.research_benchmark.verify import _environment


def test_catalogue_coverage_and_honest_family_holdout():
    index = catalogue()
    assert index["counts"] == {"cases": 42, "families": 39, "domains": 3, "splits": {"train": 12, "development": 6, "test": 24}}
    families = defaultdict(set)
    for case in CASES:
        families[case.family].add(case.split)
    assert all(len(splits) == 1 for splits in families.values())
    assert index["shared_families"] == {
        "wire-constructor-generations": ["rv2-015", "rv2-016"],
        "distribution-resource-selection": ["rv2-033", "rv2-034"],
        "csv-record-boundaries": ["rv2-007", "rv2-036"],
    }
    for domain in {case.domain for case in CASES}:
        assert Counter(case.split for case in CASES if case.domain == domain) == {"train": 4, "development": 2, "test": 8}
    assert {"dependency", "import-origin", "configuration", "parsing", "encoding", "timezone", "rounding", "resource-origin", "metadata"} <= {tag for case in CASES for tag in case.tags}
    assert index["coarse_tags_crossing_splits"]  # Specific-family holdout is not broad-concept holdout.
    assert "synthetic" in index["provenance"]
    assert "not project-held-out" in index["family_scope"]


def test_hard_study_cases_are_distinct_and_reference_reversible():
    hard = (*HARD_DEVELOPMENT_CASES, *HARD_TEST_CASES)
    assert len(HARD_DEVELOPMENT_CASES) == 2
    assert len(HARD_TEST_CASES) == 12
    assert len({case.id for case in hard}) == len(hard)
    assert {case.family for case in HARD_DEVELOPMENT_CASES}.isdisjoint(
        {case.family for case in HARD_TEST_CASES}
    )
    fingerprints = set()
    for case in hard:
        broken = snapshot_files(case.id)
        healthy = snapshot_files(case.id, correct_baseline=True)
        assert broken != healthy
        repaired = dict(broken)
        for change in reference_edits(case.id):
            change.apply(repaired)
        assert repaired == healthy
        fingerprints.add(tree_sha256(broken))
    assert len(fingerprints) == len(hard)
    for case in HARD_DEVELOPMENT_CASES:
        assert not any(path.startswith("context/dependencies/") for path in snapshot_files(case.id))


def test_operator_index_matches_executable_catalogue():
    path = Path(__file__).resolve().parents[1] / "benchmarks/research_v2/catalogue.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    assert len(index["cases"]) == len(CASES)
    for row, case in zip(index["cases"], CASES, strict=True):
        assert row == {key: value for key, value in case.summary().items() if key in {"id", "split", "domain", "family"} or (key == "variant" and value is not None)}
        seed = get_seed(case)
        assert index["project_test_counts"][case.domain] == {"public": len(seed.public), "oracle_per_distribution": len(seed.oracle)}


def test_all_reference_solutions_reverse_exact_validated_anchors():
    baseline_hashes = set()
    broken_hashes = set()
    for case in CASES:
        broken = snapshot_files(case.id)
        healthy = snapshot_files(case.id, correct_baseline=True)
        assert broken != healthy
        repaired = dict(broken)
        for change in reference_edits(case.id):
            change.apply(repaired)
        assert repaired == healthy
        assert tree_sha256(snapshot_files(case.id)) == tree_sha256(broken)
        baseline_hashes.add(tree_sha256(healthy))
        broken_hashes.add(tree_sha256(broken))
    assert len(baseline_hashes) == 4  # Three projects, with two service API environments.
    assert len(broken_hashes) == 42


def test_compressed_fixture_has_host_independent_header():
    import gzip
    from buildtrace.research_benchmark.ingestion import compressed_fixture

    first = compressed_fixture(b"id,amount\na,1\n")
    assert first[:10] == bytes.fromhex("1f8b08000000000000ff")
    assert gzip.decompress(first) == b"id,amount\na,1\n"
    assert first == compressed_fixture(b"id,amount\na,1\n")


@pytest.mark.parametrize("contents", [b"no match here", b"anchor anchor"])
def test_ambiguous_or_missing_mutation_anchors_fail_loudly(contents):
    with pytest.raises(AnchorError, match="expected exactly one anchor"):
        Edit("app.py", "anchor", "replacement").apply({"app.py": contents})
    with pytest.raises(AnchorError, match="Missing edit target"):
        Edit("missing.py", "anchor", "replacement").apply({})


def test_materialization_anchor_drift_writes_nothing(tmp_path, monkeypatch):
    from buildtrace.research_benchmark import materialize

    original = materialize.get_seed

    def drifted(case):
        seed = original(case)
        files = dict(seed.files)
        files["src/bench_app/app.py"] = b"# a changed upstream seed\n"
        return replace(seed, files=files)

    monkeypatch.setattr(materialize, "get_seed", drifted)
    with pytest.raises(AnchorError):
        materialize_case("rv2-001", tmp_path / "project", verifier_dir=tmp_path / "oracle")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("case_id", ["rv2-001", "rv2-015", "rv2-016", "rv2-029"])
def test_snapshot_is_allowlisted_and_oracle_is_separate(tmp_path, monkeypatch, case_id):
    # A nearby operator file must not be copied, even if it is in the cwd.
    sentinel = tmp_path / "private-operator-notes.txt"
    sentinel.write_text("operator-only-sentinel-6871", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = materialize_case(case_id, tmp_path / "project", verifier_dir=tmp_path / "verifier")
    actual = {path.relative_to(result.destination).as_posix(): path.read_bytes() for path in result.destination.rglob("*") if path.is_file()}
    assert actual == snapshot_files(case_id)
    for name, data in actual.items():
        assert "oracle" not in name
        assert "manifest.json" not in name
        assert "research_benchmark" not in name
        for forbidden in (b"operator-only-sentinel-6871", b"rv2-", b"reference_edits", b"WrongRepair", b"mutation", b"oracle-only"):
            assert forbidden not in data
    assert (result.verifier_dir / "tests/test_oracle.py").is_file()
    assert (result.verifier_dir / "fixtures/checks.json").is_file()
    assert load_contract(result.verifier_dir) == result.contract
    assert VerificationContract.from_dict(result.contract.to_dict()) == result.contract
    assert result.contract.snapshot_sha256 == tree_sha256(actual)
    public_defs = [node.name for node in ast.parse(actual["tests/test_application.py"]).body if isinstance(node, ast.FunctionDef)]
    oracle_defs = [node.name for node in ast.parse((result.verifier_dir / "tests/test_oracle.py").read_bytes()).body if isinstance(node, ast.FunctionDef)]
    assert len(public_defs) == len(set(public_defs)) == result.contract.expected_public_tests
    assert len(oracle_defs) == len(set(oracle_defs)) == result.contract.expected_oracle_tests
    assert set(public_defs).isdisjoint(oracle_defs)
    for name, digest in result.contract.protected_sha256.items():
        assert sha256(actual[name]) == digest
    assert "src/bench_app/data/policy.json" in result.contract.protected_sha256
    assert "src/bench_app/app.py" not in result.contract.protected_sha256


def test_new_directories_only_and_disjoint_verifier(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    note = existing / "work.txt"
    note.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        materialize_case("rv2-001", existing)
    assert note.read_text(encoding="utf-8") == "preserve"
    for project, oracle in ((tmp_path / "p", tmp_path / "p/oracle"), (tmp_path / "o/p", tmp_path / "o"), (tmp_path / "same", tmp_path / "same")):
        with pytest.raises(ValueError, match="disjoint"):
            materialize_case("rv2-001", project, verifier_dir=oracle)
    assert not (tmp_path / "p").exists()
    with pytest.raises(FileNotFoundError, match="parent"):
        materialize_case("rv2-001", tmp_path / "missing/p")


@pytest.mark.parametrize("case_id", ["rv2-001", "rv2-015", "rv2-016", "rv2-029"])
def test_healthy_seeds_pass_complete_offline_contract(tmp_path, case_id):
    before = {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}
    result = materialize_baseline(case_id, tmp_path / "project", verifier_dir=tmp_path / "verifier")
    verification = verify_snapshot(result.destination, result.verifier_dir, work_dir=tmp_path)
    if case_id == "rv2-001" and not verification.passed:
        print("Offline public diagnostic:", verification.phases[-1].detail, flush=True)
    assert verification.passed, verification.to_dict()
    assert [phase.phase for phase in verification.phases] == list(result.contract.required_phases)
    counts = {phase.phase: phase.tests for phase in verification.phases}
    assert counts["public"] == result.contract.expected_public_tests
    assert counts["wheel_oracle"] == counts["sdist_oracle"] == result.contract.expected_oracle_tests
    for phase in verification.phases:
        if phase.phase in {"wheel_oracle", "sdist_oracle"}:
            assert "Installed origin:" in phase.detail
            assert ".whl" in phase.detail
    after = {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}
    assert after == before  # Dry-run resolution never installs into the shared venv.


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_every_broken_variant_is_rejected(tmp_path, case):
    result = materialize_case(case.id, tmp_path / "project", verifier_dir=tmp_path / "verifier")
    verification = verify_snapshot(result.destination, result.verifier_dir, work_dir=tmp_path)
    assert not verification.passed
    failed = [phase for phase in verification.phases if not phase.passed]
    assert len(failed) == 1, verification.to_dict()
    expected = {"rv2-021": "resolve", "rv2-029": "wheel_oracle", "rv2-033": "wheel_oracle", "rv2-034": "sdist_oracle"}.get(case.id, "public")
    assert failed[0].phase == expected, verification.to_dict()
    assert verification.phases[0].passed  # Failure is behavioral, not a broken protection manifest.


WRONG_REPAIRS = [(case.id, repair) for case in CASES for repair in case.wrong_repairs]


@pytest.mark.parametrize("case_id,repair", WRONG_REPAIRS, ids=[f"{case_id}-{repair.name}" for case_id, repair in WRONG_REPAIRS])
def test_plausible_wrong_repairs_are_rejected(tmp_path, case_id, repair):
    result = materialize_case(case_id, tmp_path / "project", verifier_dir=tmp_path / "verifier", wrong_repair=repair.name)
    verification = verify_snapshot(result.destination, result.verifier_dir, work_dir=tmp_path)
    assert not verification.passed
    failed = next(phase for phase in verification.phases if not phase.passed)
    expected = "wheel_oracle" if repair.rejection == "oracle" else repair.rejection
    assert failed.phase == expected, verification.to_dict()
    if repair.rejection == "oracle":
        assert next(phase for phase in verification.phases if phase.phase == "public").passed


@pytest.mark.parametrize("change", ["protected-test", "protected-context", "unexpected-config", "oracle-test"])
def test_protected_or_unexpected_files_are_rejected_before_execution(tmp_path, monkeypatch, change):
    from buildtrace.research_benchmark import verify

    result = materialize_baseline("rv2-001", tmp_path / "project", verifier_dir=tmp_path / "verifier")
    path = {
        "protected-test": result.destination / "tests/test_application.py",
        "protected-context": result.destination / "context/deployment.json",
        "unexpected-config": result.destination / "pytest.ini",
        "oracle-test": result.verifier_dir / "tests/test_oracle.py",
    }[change]
    path.write_text("changed", encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("Subprocess ran before protection checks")

    monkeypatch.setattr(verify, "_run", forbidden)
    verification = verify_snapshot(result.destination, result.verifier_dir, contract=result.contract, work_dir=tmp_path)
    assert not verification.passed
    assert verification.phases[0].phase == "protected"
    assert not verification.phases[0].passed


def test_exact_original_test_count_is_required(tmp_path):
    result = materialize_baseline("rv2-001", tmp_path / "project", verifier_dir=tmp_path / "verifier")
    contract = replace(result.contract, expected_public_tests=result.contract.expected_public_tests + 1)
    verification = verify_snapshot(result.destination, result.verifier_dir, contract=contract, work_dir=tmp_path)
    assert not verification.passed
    failed = verification.phases[-1]
    assert failed.phase == "public" and failed.returncode == 0
    assert failed.tests == result.contract.expected_public_tests


def test_common_editable_dev_install_works_in_an_isolated_target(tmp_path):
    result = materialize_baseline("rv2-001", tmp_path / "project")
    site = tmp_path / "isolated-site"
    environment = _environment()
    command = [sys.executable, "-m", "pip", "install", "--no-index", "--no-build-isolation", "--no-deps", "--target", str(site), "-e", ".[dev]"]
    installed = subprocess.run(command, cwd=result.destination, env=environment, capture_output=True, text=True, encoding="utf-8")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert (site / "bench_app_local.pth").read_text(encoding="utf-8").strip() == str(result.destination / "src")
    code = "import runpy,site,sys; site.addsitedir(sys.argv.pop(1)); runpy.run_module('bench_app',run_name='__main__')"
    cli = subprocess.run([sys.executable, "-c", code, str(site), str(result.destination / "examples/input.json")], cwd=tmp_path, env=environment, capture_output=True, text=True, encoding="utf-8")
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert json.loads(cli.stdout) == get_seed(get_case("rv2-001")).example_output


@pytest.mark.parametrize("case_id,healthy", [("rv2-029", True), ("rv2-029", False), ("rv2-033", False), ("rv2-034", False)])
def test_generic_workflow_has_public_release_diagnostics(tmp_path, case_id, healthy):
    result = materialize_case(case_id, tmp_path / "project", correct_baseline=healthy)
    environment = _environment((result.destination / "src",), result.destination)
    environment.update({"TMP": str(tmp_path), "TEMP": str(tmp_path), "TMPDIR": str(tmp_path)})
    build = subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--sdist"], cwd=result.destination, env=environment, capture_output=True, text=True, encoding="utf-8")
    assert build.returncode == 0, build.stdout + build.stderr
    probe = subprocess.run([sys.executable, "ci/check_distributions.py"], cwd=result.destination, env=environment, capture_output=True, text=True, encoding="utf-8")
    assert (probe.returncode == 0) == healthy, probe.stdout + probe.stderr
    if not healthy:
        assert "installed distribution failed" in probe.stderr


def test_cli_emits_operator_metadata_outside_snapshot(tmp_path, capsys):
    from buildtrace.research_benchmark.__main__ import main

    assert main(["materialize", "rv2-005", str(tmp_path / "project"), "--verifier-dir", str(tmp_path / "verifier"), "--correct-baseline"]) == 0
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["correct_baseline"] is True
    assert metadata["case_id"] == "rv2-005"
    assert metadata["contract"]["expected_public_tests"] > 0
    assert not (tmp_path / "project/manifest.json").exists()
