import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import zipfile

import httpx
import pytest
import yaml

from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.backend import BackendError, GitHubTrialBackend
from buildtrace.models import RunReference, SourceFile
from buildtrace.research_benchmark import materialize_baseline, materialize_case

from experiment_fixtures import experiment_spec


ROOT = Path(__file__).parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "infra/research_v2" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOSTED = _load("research_hosted_runtime", "hosted_verify.py")
PREPARE = _load("research_hosted_prepare", "prepare_hosted.py")
CANDIDATE, ORACLE = "e" * 40, "a" * 40


def _prepare(root, case_id="rv2-015", *, healthy=True, wrong_repair=None):
    generated = materialize_case(case_id, root / "generated", verifier_dir=root / "bundle", correct_baseline=healthy, wrong_repair=wrong_repair)
    public, trusted = root / "public", root / "trusted"
    fields = PREPARE.prepare_deployment(generated.destination, generated.verifier_dir, public, trusted)
    return public, trusted, fields


def test_hosted_publish_retries_only_identical_stage_and_never_replaces_destination(tmp_path, monkeypatch):
    original = Path.rename
    waits, injected = [], []

    def wait(delay):
        waits.append(delay)
        time.sleep(delay)

    def locked(path, target):
        if path.name.startswith(".hosted-") and not injected:
            injected.append(str(path))
            raise PermissionError("Transient scanner lock")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", locked)
    monkeypatch.setattr(PREPARE, "time", SimpleNamespace(sleep=wait))
    public, trusted, fields = _prepare(tmp_path, "rv2-001")
    assert injected and waits and sum(waits) <= 0.75
    raw_contract = (trusted / HOSTED.CONTRACT_PATH).read_bytes()
    assert HOSTED.sha256(raw_contract) == fields["verification_contract_sha256"]
    before = (public / HOSTED.PUBLIC_DESCRIPTOR).read_bytes()
    with pytest.raises(FileExistsError):
        PREPARE._publish(tmp_path / "missing-stage", public)
    assert (public / HOSTED.PUBLIC_DESCRIPTOR).read_bytes() == before


def _execute(root, public, trusted, monkeypatch, *, oracle=True):
    root.mkdir(parents=True, exist_ok=True)
    workflow_sha = ORACLE if oracle else CANDIDATE
    monkeypatch.setattr(HOSTED, "git_sha", lambda path: workflow_sha if path == trusted else CANDIDATE)
    verifier = HOSTED.HostedVerifier(public, trusted, root / "evidence", root / "work")
    code = verifier.run(oracle_mode=oracle, source_sha=CANDIDATE, workflow_sha=workflow_sha, run_id=102 if oracle else 101, run_attempt=1)
    return code, verifier


@pytest.fixture(scope="module")
def hosted_deploy(tmp_path_factory):
    root = tmp_path_factory.mktemp("hosted-deploy")
    public, trusted, fields = _prepare(root)
    return public, trusted, fields


@pytest.fixture(scope="module")
def hosted_healthy(hosted_deploy):
    public, trusted, fields = hosted_deploy
    root = public.parent
    target = root / "public-target"
    shutil.copytree(public, target)
    # Execute just one healthy generator case, once per real hosted mode.
    with pytest.MonkeyPatch.context() as patch:
        code, oracle = _execute(root / "oracle-run", public, trusted, patch)
        assert code == 0, (oracle.artifacts / "phase-results.json").read_text(encoding="utf-8")
        code, target_verifier = _execute(root / "target-run", target, trusted, patch, oracle=False)
        assert code == 0, (target_verifier.artifacts / "phase-results.json").read_text(encoding="utf-8")
    return public, trusted, fields, oracle, target_verifier


def _zip_tree(directory):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in directory.rglob("*"):
            if path.is_file():
                bundle.writestr(path.relative_to(directory).as_posix(), path.read_bytes())
    return output.getvalue()


def _archive_members(content):
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        return {info.filename: bundle.read(info) for info in bundle.infolist()}


def _zip_members(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in members.items():
            bundle.writestr(name, content)
    return output.getvalue()


def _backend(tmp_path, collected_run, fields, handler=None):
    store = EvidenceStore(tmp_path)
    capture = store.save(collected_run)
    task = experiment_spec().tasks[0].model_copy(update=fields | {
        "base_sha": collected_run.source_ref, "capture_id": capture["id"],
        "initial_run_url": collected_run.reference.html_url, "oracle_sha": ORACLE,
    })
    if handler is None:
        handler = lambda request: pytest.fail(f"Unexpected live request: {request.method} {request.url.path}")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GitHubTrialBackend(task, store, token="offline-token", client=client, allow_publish=True, namespace="hosted-offline")


def _capture(backend, collected_run, archive, *, oracle=True):
    result = copy.deepcopy(collected_run)
    identifier = 102 if oracle else 101
    result.reference = RunReference.parse(f"https://github.com/{backend.task.repository}/actions/runs/{identifier}/attempts/1")
    result.source_ref = ORACLE if oracle else CANDIDATE
    result.run.update(id=identifier, run_attempt=1, head_sha=result.source_ref, conclusion="success", status="completed", event="workflow_dispatch")
    result.jobs = [{"name": name, "conclusion": "success", "steps": [{"name": step, "conclusion": "success"} for step in steps]} for name, steps in backend.task.required_steps.items()]
    result.attachments = [SourceFile("oracle-evidence.zip" if oracle else "public-evidence.zip", archive)]
    return backend.store.save(result)


def test_hosted_generator_healthy_separate_counts_offline_wirekit_and_origin(hosted_healthy, tmp_path, collected_run):
    public, trusted, fields, oracle, target = hosted_healthy
    assert fields["expected_test_count"] != fields["expected_oracle_test_count"]
    proof = json.loads((oracle.artifacts / "verification-proof.json").read_bytes())
    assert proof["verification_complete"] and proof["oracle_mode"]
    assert [phase["phase"] for phase in proof["phases"]] == list(HOSTED.PHASES)
    assert all(phase["passed"] for phase in proof["phases"])
    phases = {phase["phase"]: phase for phase in proof["phases"]}
    assert "--no-index" in phases["resolve"]["command"] and "--dry-run" in phases["resolve"]["command"]
    for name in ("wheel_oracle", "sdist_oracle"):
        assert phases[name]["tests"] == fields["expected_oracle_test_count"]
        assert phases[name]["origin"]["verified"]
        assert ".whl" in phases[name]["origin"]["module_file"]
    assert proof["verification_contract_sha256"] == fields["verification_contract_sha256"]
    assert json.loads((oracle.artifacts / "phase-results.json").read_bytes())["dependencies"]
    assert not (target.artifacts / "verification-contract.json").exists()
    assert not (public / "infra/research_v2/oracle").exists()
    backend = _backend(tmp_path, collected_run, fields)
    try:
        assert backend._oracle_passed(_capture(backend, collected_run, _zip_tree(oracle.artifacts)), CANDIDATE)
        assert backend._hosted_failures(_capture(backend, collected_run, _zip_tree(target.artifacts), oracle=False), CANDIDATE, oracle=False) == []
    finally:
        backend.client.close()


@pytest.mark.parametrize("case_id,wrong,failed", [
    ("rv2-029", None, "wheel_oracle"),
    ("rv2-024", "fixed-date-fallback", "wheel_oracle"),
    ("rv2-034", None, "sdist_oracle"),
])
def test_hosted_rejects_source_only_wrong_public_pass_and_sdist_candidates(tmp_path, monkeypatch, case_id, wrong, failed):
    public, trusted, fields = _prepare(tmp_path, case_id, healthy=False, wrong_repair=wrong)
    code, verifier = _execute(tmp_path / "execution", public, trusted, monkeypatch)
    proof = json.loads((verifier.artifacts / "verification-proof.json").read_bytes())
    phases = {phase["phase"]: phase for phase in proof["phases"]}
    assert code == 1 and proof["verification_complete"] is False
    assert phases["public"]["passed"] and phases["public"]["tests"] == fields["expected_test_count"]
    assert not phases[failed]["passed"]
    if wrong:
        assert proof["public_distribution"]["passed"]  # The oracle supplies a genuinely stronger check.
    if failed == "sdist_oracle":
        assert phases["wheel_oracle"]["passed"] and phases["sdist_rebuild"]["passed"]


def test_hosted_sdist_missing_backend_cannot_claim_complete_success(tmp_path, monkeypatch):
    public, trusted, _ = _prepare(tmp_path, "rv2-001")
    metadata = public / "pyproject.toml"
    original = metadata.read_text(encoding="utf-8")
    assert '"ci/*.py", ' in original
    metadata.write_text(original.replace('"ci/*.py", ', ""), encoding="utf-8")
    code, verifier = _execute(tmp_path / "execution", public, trusted, monkeypatch)
    proof = json.loads((verifier.artifacts / "verification-proof.json").read_bytes())
    phases = {phase["phase"]: phase for phase in proof["phases"]}
    assert code == 1 and phases["public"]["passed"] and phases["wheel_oracle"]["passed"]
    assert not phases["sdist_rebuild"]["passed"] and phases["sdist_oracle"]["returncode"] is None
    assert not proof["public_distribution"]["passed"] and not proof["verification_complete"]


@pytest.mark.parametrize("mismatch", [
    "missing-phase", "duplicate-phase", "failed-phase", "public-count", "oracle-count", "contract-hash", "contract-bytes",
    "candidate-sha", "workflow-sha", "run-attempt", "source-origin", "origin-hash", "same-origin", "protected-tree", "incomplete",
    "wheel-skip", "sdist-count", "missing-public-report", "distribution-bytes",
])
def test_backend_rejects_v2_phase_count_hash_origin_and_report_mismatches(hosted_healthy, tmp_path, collected_run, mismatch):
    _, _, fields, oracle, _ = hosted_healthy
    members = _archive_members(_zip_tree(oracle.artifacts))
    proof = json.loads(members["verification-proof.json"])
    phases = {phase["phase"]: phase for phase in proof["phases"]}
    if mismatch == "missing-phase":
        proof["phases"].pop()
    elif mismatch == "duplicate-phase":
        proof["phases"][-1] = proof["phases"][0]
    elif mismatch == "failed-phase":
        phases["resolve"]["passed"] = False
    elif mismatch == "public-count":
        proof["expected_public_tests"] += 1
    elif mismatch == "oracle-count":
        proof["expected_oracle_tests"] += 1
    elif mismatch == "contract-hash":
        proof["verification_contract_sha256"] = "f" * 64
    elif mismatch == "contract-bytes":
        contract = json.loads(members["verification-contract.json"])
        contract["snapshot_sha256"] = "f" * 64
        members["verification-contract.json"] = HOSTED.canonical(contract)
    elif mismatch in {"candidate-sha", "workflow-sha"}:
        proof["checked_out_sha" if mismatch == "candidate-sha" else "workflow_sha"] = "f" * 40
    elif mismatch == "run-attempt":
        proof["run_attempt"] = 2
    elif mismatch == "source-origin":
        phases["wheel_oracle"]["origin"]["module_file"] = "/candidate/src/bench_app/__init__.py"
    elif mismatch == "origin-hash":
        phases["wheel_oracle"]["origin"]["wheel_sha256"] = "f" * 64
    elif mismatch == "same-origin":
        phases["sdist_oracle"]["origin"] = copy.deepcopy(phases["wheel_oracle"]["origin"])
    elif mismatch == "protected-tree":
        proof["protected_tree_sha256"] = "f" * 64
    elif mismatch == "incomplete":
        proof["verification_complete"] = False
    elif mismatch == "wheel-skip":
        members["wheel-oracle.xml"] = members["wheel-oracle.xml"].replace(b"</testcase>", b"<skipped/></testcase>", 1)
        # Pytest emits self-closing successful cases on some versions.
        if b"<skipped/>" not in members["wheel-oracle.xml"]:
            members["wheel-oracle.xml"] = members["wheel-oracle.xml"].replace(b" />", b"><skipped/></testcase>", 1)
    elif mismatch == "sdist-count":
        members["sdist-oracle.xml"] = b'<testsuite tests="1"><testcase name="one"/></testsuite>'
    elif mismatch == "missing-public-report":
        del members["pytest.xml"]
    elif mismatch == "distribution-bytes":
        name = "distributions/direct/" + proof["distributions"]["direct"]["filename"]
        members[name] += b"changed-after-origin-proof"
    members["verification-proof.json"] = HOSTED.canonical(proof)
    backend = _backend(tmp_path, collected_run, fields)
    try:
        capture = _capture(backend, collected_run, _zip_members(members))
        assert not backend._oracle_passed(capture, CANDIDATE), mismatch
    finally:
        backend.client.close()


@pytest.mark.parametrize("mismatch", [None, "contract-hash", "oracle-count", "runtime", "oracle-file", "missing-contract"])
def test_pin_validates_deployed_contract_runtime_and_separate_bundle(hosted_deploy, tmp_path, collected_run, mismatch):
    public, trusted, fields = hosted_deploy
    fields = dict(fields)
    if mismatch == "contract-hash":
        fields["verification_contract_sha256"] = "f" * 64
    if mismatch == "oracle-count":
        fields["expected_oracle_test_count"] += 1
    if mismatch == "missing-contract":
        fields["oracle_contract_path"] = "infra/research_v2/missing.json"
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if "/git/ref/" in path:
            return httpx.Response(200, json={"object": {"sha": ORACLE}})
        relative = path.split("/contents/", 1)[1]
        root = trusted if request.url.params["ref"] == ORACLE else public
        source = root / relative
        if not source.is_file():
            return httpx.Response(404)
        content = source.read_bytes()
        if mismatch == "runtime" and relative == HOSTED.RUNTIME_PATH or mismatch == "oracle-file" and relative.endswith("tests/test_oracle.py"):
            content += b"\n# changed\n"
        return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": base64.b64encode(content).decode()})

    backend = _backend(tmp_path, collected_run, fields, handler)
    try:
        if mismatch:
            with pytest.raises(BackendError):
                backend.pin_oracle()
        else:
            pinned = backend.pin_oracle()
            assert pinned.verification_contract_sha256 == fields["verification_contract_sha256"]
            assert backend._supports_dispatch_id and backend._expected_oracle_tests() == fields["expected_oracle_test_count"]
            calls.clear()
            with pytest.raises(ValueError, match="does not exist"):
                backend.read_file("infra/research_v2/oracle/tests/test_oracle.py")
            assert len(calls) == 1 and calls[0].url.params["ref"] == backend.task.base_sha
            assert all(request.url.params.get("ref") != ORACLE for request in calls)
    finally:
        backend.client.close()


def test_deployment_is_self_contained_and_public_tree_excludes_operator_bundle(hosted_deploy, tmp_path):
    public, trusted, fields = hosted_deploy
    descriptor = json.loads((public / HOSTED.PUBLIC_DESCRIPTOR).read_bytes())
    assert set(descriptor) == {"schema_version", "expected_public_tests"}
    for path in public.rglob("*"):
        name = path.relative_to(public).as_posix()
        if path.is_file() and not HOSTED.ignored(name):
            assert "manifest.json" not in name and "test_oracle.py" not in name
            assert b"reference_edits" not in path.read_bytes() and b"rv2-015" not in path.read_bytes()
    assert (trusted / "infra/research_v2/oracle/fixtures/checks.json").is_file()
    helper = public / HOSTED.RUNTIME_PATH
    result = subprocess.run([sys.executable, "-S", str(helper), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    document = yaml.load((public / ".github/workflows/ci.yml").read_bytes(), Loader=yaml.BaseLoader)
    job = document["jobs"]["checks"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in job["steps"] if "uses" in step)
    assert set(fields["required_steps"]["checks"]) <= {step["name"] for step in job["steps"]}
    assert document["on"]["push"]["branches"] == ["buildtrace-inputs/**"]
    assert "buildtrace" not in job["steps"][3]["run"] and "wikitrace" not in job["steps"][3]["run"]


def test_pin_fills_optional_v2_contract_hash_and_separate_count(hosted_deploy, tmp_path, collected_run):
    _, trusted, fields = hosted_deploy

    def handler(request):
        if "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": ORACLE}})
        path = request.url.path.split("/contents/", 1)[1]
        return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": base64.b64encode((trusted / path).read_bytes()).decode()})

    optional = fields | {"verification_contract_sha256": None, "expected_oracle_test_count": None}
    backend = _backend(tmp_path, collected_run, optional, handler)
    try:
        pinned = backend.pin_oracle()
        assert pinned.verification_contract_sha256 == fields["verification_contract_sha256"]
        assert pinned.expected_oracle_test_count == fields["expected_oracle_test_count"] != fields["expected_test_count"]
    finally:
        backend.client.close()


def test_runtime_finalization_rejects_changed_built_distribution(hosted_healthy, tmp_path, monkeypatch):
    public, trusted, _, oracle, _ = hosted_healthy
    evidence, work = tmp_path / "evidence", tmp_path / "work"
    shutil.copytree(oracle.artifacts, evidence)
    state = json.loads((evidence / "phase-results.json").read_bytes())
    old = Path(state["distributions"]["direct"]["path"])
    changed = tmp_path / old.name
    changed.write_bytes(old.read_bytes() + b"changed-after-tests")
    state["distributions"]["direct"]["path"] = str(changed)
    (evidence / "phase-results.json").write_bytes(HOSTED.canonical(state))
    monkeypatch.setattr(HOSTED, "git_sha", lambda path: ORACLE if path == trusted else CANDIDATE)
    verifier = HOSTED.HostedVerifier(public, trusted, evidence, work)
    assert verifier.finalize() == 1
    assert json.loads((evidence / "verification-proof.json").read_bytes())["verification_complete"] is False


def test_offline_resolve_rejects_remote_dependencies_before_starting_pip(tmp_path, monkeypatch):
    public, trusted, _ = _prepare(tmp_path)
    monkeypatch.setattr(HOSTED, "git_sha", lambda path: ORACLE if path == trusted else CANDIDATE)
    verifier = HOSTED.HostedVerifier(public, trusted, tmp_path / "evidence", tmp_path / "work")
    assert verifier.initialize(oracle_mode=True, source_sha=CANDIDATE, workflow_sha=ORACLE, run_id=102, run_attempt=1) == 0
    metadata = public / "pyproject.toml"
    text = metadata.read_text(encoding="utf-8")
    assert 'dependencies = [' in text
    import tomllib

    dependency = tomllib.loads(text)["project"]["dependencies"][0]
    metadata.write_text(text.replace(dependency, "wirekit @ https://invalid.example/wirekit.whl"), encoding="utf-8")
    monkeypatch.setattr(HOSTED.subprocess, "run", lambda *args, **kwargs: pytest.fail("Offline resolution must reject URLs before invoking pip"))
    assert verifier.phase("resolve") == 1
    phase = json.loads((verifier.artifacts / "phase-results.json").read_bytes())["phases"][-1]
    assert "cannot reference URLs" in phase["detail"]


def test_prepare_refuses_changed_protected_inputs_and_preserves_existing_outputs(tmp_path):
    generated = materialize_baseline("rv2-001", tmp_path / "generated", verifier_dir=tmp_path / "bundle")
    (generated.destination / "context/project.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Protected file"):
        PREPARE.prepare_deployment(generated.destination, generated.verifier_dir, tmp_path / "public", tmp_path / "trusted")
    assert not (tmp_path / "public").exists() and not (tmp_path / "trusted").exists()
    (tmp_path / "public").mkdir()
    note = tmp_path / "public/user-work.txt"
    note.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        PREPARE.prepare_deployment(generated.destination, generated.verifier_dir, tmp_path / "public", tmp_path / "trusted")
    assert note.read_text(encoding="utf-8") == "preserve"
