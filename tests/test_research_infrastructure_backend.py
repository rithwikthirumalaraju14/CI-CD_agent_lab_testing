import base64
import copy
import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.backend import BackendError, GitHubTrialBackend, junit_passed
from buildtrace.github import GetRetryPolicy, GitHubActionsClient, GitHubError
from buildtrace.models import RunReference, SourceFile

from conftest import log_zip
from experiment_fixtures import experiment_spec


CANDIDATE = "e" * 40
ORACLE = "a" * 40
SOURCE_PATH = "src/bench_app/main.py"


@pytest.fixture
def backend(tmp_path, collected_run):
    store = EvidenceStore(tmp_path)
    initial = store.save(collected_run)
    task = experiment_spec().tasks[0].model_copy(update={
        "base_sha": collected_run.source_ref, "initial_run_url": collected_run.reference.html_url,
        "capture_id": initial["id"], "oracle_sha": ORACLE, "expected_test_count": 2,
        "required_steps": {"bench-checks": ["Run tests", "Build package", "Run example"]},
    })

    def unexpected(request):
        pytest.fail(f"Unexpected offline request: {request.method} {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as http:
        instance = GitHubTrialBackend(
            task, store, token="offline-token", client=http, allow_publish=True, namespace="offline-trial",
            artifact_prefix="bench-ci", retry_policy=GetRetryPolicy(base_delay_seconds=0, max_delay_seconds=0, max_wait_seconds=0),
        )
        instance.originals[SOURCE_PATH] = "value = 0\n"
        yield instance


def _xml(count=2, failure=False):
    cases = "".join(f'<testcase name="test_{number}">{"<failure/>" if failure and number == 0 else ""}</testcase>' for number in range(count))
    return f'<testsuites><testsuite tests="{count}" failures="{int(failure)}" errors="0" skipped="0">{cases}</testsuite></testsuites>'


def _archive(backend, run_id=102, *, proof_updates=None, proof=True, count=2, failure=False, environment_sha=CANDIDATE):
    document = {
        "schema_version": 1, "checked_out_sha": CANDIDATE, "workflow_sha": ORACLE, "oracle_mode": True,
        "expected_test_count": 2, "observed_test_count": 2, "protected_tests_sha256": "d" * 64,
        "protected_tests_unchanged": True, "verification_complete": True, "run_id": run_id, "run_attempt": 1,
    }
    document.update(proof_updates or {})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("evidence/pytest.xml", _xml(count, failure))
        bundle.writestr("evidence/environment-before-install.json", json.dumps({"checked_out_sha": environment_sha}))
        if proof:
            bundle.writestr("evidence/verification-proof.json", json.dumps(document))
    return stream.getvalue()


def _collected(backend, collected_run, run_id, *, oracle=False, conclusion="success", archive=None):
    result = copy.deepcopy(collected_run)
    result.reference = RunReference.parse(f"https://github.com/{backend.task.repository}/actions/runs/{run_id}/attempts/1")
    result.source_ref = ORACLE if oracle else CANDIDATE
    result.run.update(id=run_id, run_attempt=1, head_sha=result.source_ref, event="workflow_dispatch", status="completed", conclusion=conclusion)
    result.jobs = [{"name": name, "conclusion": conclusion, "steps": [{"name": step, "conclusion": conclusion} for step in steps]} for name, steps in backend.task.required_steps.items()]
    result.log_archive = log_zip({"bench-checks/Run tests.txt": b"original offline CI diagnostics\n"})
    result.attachments = [SourceFile("oracle-evidence.zip", archive)] if archive is not None else []
    result.artifacts = []
    result.warnings = []
    return result


def _candidate_api(backend, calls):
    def api(method, path, payload=None):
        calls.append((method, path, payload))
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": ORACLE}}
        if method == "GET" and path.startswith("/git/commits/"):
            return {"tree": {"sha": "b" * 40}}
        if path == "/git/blobs":
            return {"sha": "1" * 40}
        if path == "/git/trees":
            return {"sha": "2" * 40}
        if path == "/git/commits":
            return {"sha": CANDIDATE}
        if path == "/git/refs":
            return {}
        pytest.fail(f"Unexpected candidate operation: {method} {path}")

    return api


def test_generic_artifact_prefix_and_proof_contract(backend, collected_run):
    archive = _archive(backend)
    collected = _collected(backend, collected_run, 102, oracle=True)
    collected.artifacts = [
        {"id": 7, "name": "invoice-ci-102-1"},
        {"id": 8, "name": "bench-ci-102-2"},
        {"id": 9, "name": "bench-ci-102-1", "expired": True},
        {"id": 10, "name": "bench-ci-102-1", "expired": False},
    ]
    downloads = []
    client = SimpleNamespace(_download_logs=lambda path: downloads.append(path) or archive)
    backend._attach_oracle_evidence(collected, client)
    assert downloads == ["/repos/example/invoice/actions/artifacts/10/zip"]
    capture = backend.store.save(collected)
    assert backend.store.verify(capture["id"])["integrity"] == "verified"
    assert backend._oracle_passed(capture, CANDIDATE)
    entries = {entry["label"]: entry for entry in capture["entries"] if entry["kind"] == "artifact"}
    assert {"oracle-evidence.zip", "pytest.xml", "environment-before-install.json", "verification-proof.json"} <= entries.keys()
    assert backend.store.read_entry(capture["id"], entries["oracle-evidence.zip"]["id"])[1] == archive


@pytest.mark.parametrize("updates", [
    {"schema_version": True}, {"oracle_mode": False}, {"checked_out_sha": "f" * 40},
    {"workflow_sha": "f" * 40}, {"expected_test_count": 1}, {"observed_test_count": 1},
    {"expected_test_count": True}, {"protected_tests_unchanged": False},
    {"verification_complete": False}, {"protected_tests_sha256": "invalid"},
    {"run_id": 999}, {"run_attempt": 2},
])
def test_generic_oracle_rejects_invalid_protected_proof(backend, collected_run, updates):
    capture = backend.store.save(_collected(backend, collected_run, 102, oracle=True, archive=_archive(backend, proof_updates=updates)))
    assert not backend._oracle_passed(capture, CANDIDATE)
    assert "proof" in "; ".join(backend._oracle_failures(capture, CANDIDATE))


def test_generic_oracle_rejects_missing_proof_wrong_checkout_and_skipped_checks(backend, collected_run):
    for archive in (_archive(backend, proof=False), _archive(backend, environment_sha="f" * 40), _archive(backend, count=1)):
        capture = backend.store.save(_collected(backend, collected_run, 102, oracle=True, archive=archive))
        assert not backend._oracle_passed(capture, CANDIDATE)
    capture = backend.store.save(_collected(backend, collected_run, 102, oracle=True, archive=_archive(backend)))
    capture["jobs"][0]["steps"][0]["conclusion"] = "skipped"
    assert not backend._oracle_passed(capture, CANDIDATE)
    capture["jobs"][0]["steps"][0]["conclusion"] = "success"
    capture["run"]["head_sha"] = "f" * 40
    assert not backend._oracle_passed(capture, CANDIDATE)


def test_invoice_pilot_artifacts_still_accept_original_sha_and_junit_contract(backend, collected_run):
    backend.artifact_prefix = "invoice-ci"
    capture = backend.store.save(_collected(backend, collected_run, 102, oracle=True, archive=_archive(backend, proof=False)))
    assert backend._oracle_passed(capture, CANDIDATE)


@pytest.mark.parametrize("child", ["failure", "error", "skipped"])
def test_junit_cannot_hide_failed_cases_behind_green_suite_totals(child):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("pytest.xml", f'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="original"><{child}/></testcase></testsuite>')
    assert not junit_passed(stream.getvalue(), 1)


def test_pin_generic_workflow_discovers_unique_dispatch_contract(backend):
    workflow = (Path(__file__).parents[1] / "infra/research_v2/trusted-ci.yml").read_bytes()
    calls = []

    def handler(request):
        calls.append(request)
        if "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": ORACLE}})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": base64.b64encode(workflow).decode()})

    with httpx.Client(transport=httpx.MockTransport(handler)) as backend.client:
        pinned = backend.pin_oracle()
    assert pinned.oracle_sha == ORACLE and pinned.capture_sha256 is not None
    assert backend._supports_dispatch_id and len(calls) == 3


def test_lost_dispatch_acknowledgement_is_reconciled_without_duplicate_post(backend, collected_run, monkeypatch):
    backend._supports_dispatch_id = True
    calls, dispatched = [], {}

    def row(identifier, title):
        return {"id": identifier, "head_sha": ORACLE, "head_branch": "main", "event": "workflow_dispatch", "display_title": title, "status": "completed", "html_url": f"https://github.com/{backend.task.repository}/actions/runs/{identifier}", "run_attempt": 1}

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert not dispatched
            dispatched.update(json.loads(request.content))
            raise httpx.ReadTimeout("acknowledgement lost", request=request)
        rows = [row(100, "older unrelated run")]
        if dispatched:
            title = f"BuildTrace oracle {CANDIDATE} {dispatched['inputs']['dispatch_id']}"
            rows += [row(101, "another trial"), row(102, title)]
        return httpx.Response(200, json={"workflow_runs": rows})

    observed = []

    def collect(client, reference):
        observed.append(reference.run_id)
        return _collected(backend, collected_run, reference.run_id, oracle=True)

    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient.collect", collect)
    inputs = {"source_sha": CANDIDATE, "oracle_mode": True}
    with httpx.Client(transport=httpx.MockTransport(handler)) as backend.client:
        capture = backend._dispatch_and_capture("main", ORACLE, inputs, time.monotonic() + 30, archive_oracle=True)
        repeated = backend._dispatch_and_capture("main", ORACLE, inputs, time.monotonic() + 30, archive_oracle=True)
    assert capture["id"] == repeated["id"] and observed == [102]
    assert inputs == {"source_sha": CANDIDATE, "oracle_mode": True}
    assert sum(request.method == "POST" for request in calls) == 1
    state = next(iter(backend._dispatches.values()))
    assert state["dispatch_error"]["diagnostics"]["ambiguous_mutation"]
    assert state["expected_title"].startswith(f"BuildTrace oracle {CANDIDATE} offline-trial-")


def test_completed_listing_waits_for_terminal_attempt_and_jobs_before_archiving(backend, collected_run, monkeypatch):
    posted = []

    def api(method, path, payload=None):
        if method == "POST":
            posted.append(payload)
            return {}
        return {"workflow_runs": [] if not posted else [{
            "id": 102, "head_sha": ORACLE, "status": "completed", "run_attempt": 1,
            "html_url": f"https://github.com/{backend.task.repository}/actions/runs/102",
        }]}

    observations = []

    def collect(client, reference):
        result = _collected(backend, collected_run, reference.run_id, oracle=True)
        observations.append(result)
        if len(observations) == 1:
            result.run.update(status="in_progress", conclusion=None)
            result.jobs[0].update(status="in_progress", conclusion=None)
        return result

    backend._api = api
    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient.collect", collect)
    monkeypatch.setattr("buildtrace.experiments.backend.time.sleep", lambda _: None)
    capture = backend._dispatch_and_capture("main", ORACLE, {}, time.monotonic() + 30)
    assert len(posted) == 1 and len(observations) == 2
    assert capture["run"]["status"] == "completed"
    assert capture["jobs"][0]["conclusion"] == "success"


def test_multiple_new_matching_runs_are_not_silently_selected_or_cancelled(backend):
    backend._supports_dispatch_id = True
    dispatched, calls = {}, []

    def api(method, path, payload=None):
        calls.append((method, path))
        if method == "POST":
            dispatched.update(payload)
            return {}
        rows = []
        if dispatched:
            title = f"BuildTrace oracle {CANDIDATE} {dispatched['inputs']['dispatch_id']}"
            rows = [{"id": identifier, "head_sha": ORACLE, "display_title": title, "status": "queued"} for identifier in (102, 103)]
        return {"workflow_runs": rows}

    backend._api = api
    with pytest.raises(BackendError, match="attribution is ambiguous") as error:
        backend._dispatch_and_capture("main", ORACLE, {"source_sha": CANDIDATE, "oracle_mode": True}, time.monotonic() + 30, archive_oracle=True)
    assert len(error.value.diagnostics["runs"]) == 2
    assert sum(method == "POST" for method, _ in calls) == 1


def test_rejected_dispatch_diagnostics_serialize_and_the_post_is_not_repeated(backend):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403) if request.method == "POST" else httpx.Response(200, json={"workflow_runs": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as backend.client:
        for _ in range(2):
            with pytest.raises(BackendError) as caught:
                backend._dispatch_and_capture("main", ORACLE, {}, time.monotonic() + 30)
            assert "403" in json.dumps(caught.value.as_dict())
    assert [request.method for request in calls] == ["GET", "POST"]


def test_failed_artifact_get_still_archives_available_ci_diagnostics(backend, collected_run, monkeypatch):
    sent = []

    def api(method, path, payload=None):
        if method == "POST":
            sent.append(payload)
            return {}
        rows = [] if not sent else [{"id": 102, "head_sha": ORACLE, "display_title": "Oracle desired", "status": "completed", "html_url": f"https://github.com/{backend.task.repository}/actions/runs/102", "run_attempt": 1}]
        return {"workflow_runs": rows}

    def collect(client, reference):
        result = _collected(backend, collected_run, 102, oracle=True)
        result.artifacts = [{"id": 10, "name": "bench-ci-102-1", "expired": False}]
        return result

    def failed_download(client, path):
        raise GitHubError("offline artifact timeout")

    backend._api = api
    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient.collect", collect)
    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient._download_logs", failed_download)
    with pytest.raises(BackendError, match="original verification artifact") as error:
        backend._dispatch_and_capture("main", ORACLE, {}, time.monotonic() + 30, expected_title="Oracle desired", archive_oracle=True)
    capture_id = error.value.diagnostics["capture_id"]
    capture = backend.store.load(capture_id)
    assert backend.store.verify(capture_id)["integrity"] == "verified"
    assert any(entry["kind"] == "log" for entry in capture["entries"])
    assert any("original verification artifact" in warning for warning in capture["warnings"])
    assert capture_id in backend.allowed_captures


def test_v2_target_dispatch_archives_public_evidence_with_its_own_label(backend, collected_run, monkeypatch):
    backend.task = backend.task.model_copy(update={"schema_version": 2})
    sent, downloads = [], []

    def api(method, path, payload=None):
        if method == "POST":
            sent.append(payload)
            return {}
        return {"workflow_runs": [] if not sent else [{
            "id": 101, "head_sha": CANDIDATE, "status": "completed", "run_attempt": 1,
            "html_url": f"https://github.com/{backend.task.repository}/actions/runs/101",
        }]}

    def collect(client, reference):
        result = _collected(backend, collected_run, 101)
        result.artifacts = [{"id": 10, "name": "bench-ci-101-1"}]
        return result

    backend._api = api
    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient.collect", collect)
    archive = _archive(backend, run_id=101)
    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient._download_logs", lambda client, path: downloads.append(path) or archive)
    capture = backend._dispatch_and_capture("buildtrace-trials/owned", CANDIDATE, {}, time.monotonic() + 30)
    labels = {entry["label"] for entry in capture["entries"] if entry["kind"] == "artifact"}
    assert "public-evidence.zip" in labels and "oracle-evidence.zip" not in labels
    assert len(sent) == len(downloads) == 1
    assert backend.store.verify(capture["id"])["integrity"] == "verified"
    assert backend._hosted_failures(capture, CANDIDATE, oracle=False)  # Archiving alone is not verification.


def test_exhausted_artifact_listing_get_is_infrastructure_not_a_bad_fix(backend, collected_run):
    artifact_gets = []

    def handler(request):
        path = request.url.path
        if path.endswith("/artifacts"):
            artifact_gets.append(request)
            return httpx.Response(503)
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": collected_run.jobs})
        if path.endswith("/logs"):
            return httpx.Response(200, content=collected_run.log_archive)
        if "/contents/" in path:
            return httpx.Response(404)
        return httpx.Response(200, json=collected_run.run)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with GitHubActionsClient(token="offline-token", client=http, retry_policy=backend.retry_policy) as client:
            collected = client.collect(collected_run.reference)
            with pytest.raises(BackendError, match="list the oracle") as caught:
                backend._attach_oracle_evidence(collected, client)
    diagnostics = caught.value.diagnostics["artifact_listing_error"]
    assert len(artifact_gets) == diagnostics["request_attempts"] == 3 and diagnostics["status_code"] == 503
    capture = backend.store.save(collected)
    assert any(entry["kind"] == "log" for entry in capture["entries"])
    assert backend.store.verify(capture["id"])["integrity"] == "verified"


def test_submission_failure_recovers_both_saved_captures_and_oracle_diagnostics(backend, collected_run, monkeypatch):
    backend._supports_dispatch_id = True
    clock, calls, dispatched = [100.0], [], {}
    candidate_api = _candidate_api(backend, calls)
    monkeypatch.setattr("buildtrace.experiments.backend.time.monotonic", lambda: clock[0])

    def api(method, path, payload=None):
        if path.startswith("/git/"):
            return candidate_api(method, path, payload)
        if method == "POST":
            kind = "oracle" if payload["inputs"].get("oracle_mode") else "target"
            dispatched[kind] = payload
            return {}
        kind = "oracle" if f"head_sha={ORACLE}" in path else "target"
        payload = dispatched.get(kind)
        if payload is None:
            return {"workflow_runs": []}
        identifier = 102 if kind == "oracle" else 101
        title = f"BuildTrace {kind} {CANDIDATE} {payload['inputs']['dispatch_id']}"
        return {"workflow_runs": [{
            "id": identifier, "head_sha": ORACLE if kind == "oracle" else CANDIDATE,
            "head_branch": payload["ref"], "event": "workflow_dispatch", "status": "completed",
            "display_title": title, "html_url": f"https://github.com/{backend.task.repository}/actions/runs/{identifier}", "run_attempt": 1,
        }]}

    def collect(client, reference):
        oracle = reference.run_id == 102
        clock[0] += 5 if oracle else 3
        result = _collected(backend, collected_run, reference.run_id, oracle=oracle)
        if oracle:
            result.artifacts = [{"id": 10, "name": "bench-ci-102-1"}]
        return result

    def failed_download(client, path):
        clock[0] += 2
        error = GitHubError("offline artifact service unavailable", 503)
        error.retry_attempts = 3
        raise error

    backend._api = api
    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient.collect", collect)
    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient._download_logs", failed_download)
    with pytest.raises(BackendError) as caught:
        backend.submit_patch([{"path": SOURCE_PATH, "content": "value = 1\n"}], timeout_seconds=60)
    error = caught.value
    assert error.elapsed_seconds == backend.ci_operation_seconds == 10
    assert error.diagnostics["status_code"] == 503 and error.diagnostics["request_attempts"] == 3
    assert error.verification.capture_id is not None and error.verification.oracle_capture_id is not None
    assert not error.verification.passed and not error.verification.oracle_passed
    assert error.observation["current_logs"] and error.observation["oracle_logs"]
    for capture_id in (error.verification.capture_id, error.verification.oracle_capture_id):
        assert backend.store.verify(capture_id)["integrity"] == "verified"
    assert set(dispatched) == {"target", "oracle"}


def test_invalid_patch_and_log_arguments_remain_recoverable_tool_errors(backend):
    backend._api = lambda *args: pytest.fail("Invalid tool inputs must not create or dispatch candidates")
    with pytest.raises(ValueError, match="current-log catalog"):
        backend.read_log(f"{backend.task.capture_id}/not-an-entry")
    with pytest.raises(ValueError, match="one and six"):
        backend.submit_patch([], timeout_seconds=10)
    with pytest.raises(ValueError, match="does not allow"):
        backend.submit_patch([{"path": "tests/test_original.py", "content": "disabled"}], timeout_seconds=10)
    assert backend.attempt == 0 and backend.last_failure is None and backend.ci_operation_seconds == 0


@pytest.mark.parametrize("failure", ["poll", "archive"])
def test_failed_verification_preserves_elapsed_time_candidate_and_prior_diagnostics(backend, collected_run, monkeypatch, failure):
    clock, calls, dispatches = [100.0], [], []
    monkeypatch.setattr("buildtrace.experiments.backend.time.monotonic", lambda: clock[0])
    backend._api = _candidate_api(backend, calls)
    target = backend.store.save(_collected(backend, collected_run, 101))

    def dispatch(ref, head, inputs, deadline, **kwargs):
        dispatches.append((ref, head))
        clock[0] += 7 if len(dispatches) == 1 else 11
        if len(dispatches) == 1:
            return target
        if failure == "archive":
            raise PermissionError("staging lock exhausted")
        raise BackendError("polling failed", diagnostics={"status_code": 503, "request_attempts": 3})

    backend._dispatch_and_capture = dispatch
    with pytest.raises(BackendError) as caught:
        backend.submit_patch([{"path": SOURCE_PATH, "content": "value = 1\n"}], timeout_seconds=60)
    error = caught.value
    assert error.elapsed_seconds == backend.ci_operation_seconds == 18
    assert error is backend.last_failure
    assert error.diagnostics["phase"] == "trusted-oracle"
    assert error.diagnostics["candidate_sha"] == CANDIDATE
    assert error.diagnostics["target_capture_id"] == target["id"]
    assert error.verification is not None and not error.verification.passed
    assert error.verification.workflow_conclusion == "success"
    assert error.verification.required_checks_passed and not error.verification.oracle_passed
    assert error.verification.elapsed_seconds == 18 and error.verification.capture_id == target["id"]
    assert error.hunks and error.observation["current_logs"]
    assert len(dispatches) == 2 and backend._operation_deadline is None
    serialized = json.dumps(error.observation)
    assert "offline-token" not in serialized and "elapsed_seconds" in serialized


def test_a_completed_bad_fix_returns_failed_verification_without_automatic_reruns(backend, collected_run):
    calls, dispatches = [], []
    backend._api = _candidate_api(backend, calls)
    target = backend.store.save(_collected(backend, collected_run, 101, conclusion="failure"))
    oracle = backend.store.save(_collected(backend, collected_run, 102, oracle=True, conclusion="failure", archive=_archive(backend, failure=True)))

    def dispatch(ref, head, inputs, deadline, **kwargs):
        dispatches.append(ref)
        return oracle if kwargs.get("archive_oracle") else target

    backend._dispatch_and_capture = dispatch
    result = backend.submit_patch([{"path": SOURCE_PATH, "content": "value = -1\n"}], timeout_seconds=60)
    assert not result.verification.passed and result.verification.workflow_conclusion == "failure"
    assert "original test count or JUnit" in result.verification.details
    assert len(dispatches) == 2 and backend.attempt == 1
    assert sum(method == "POST" and path == "/git/commits" for method, path, _ in calls) == 1


@pytest.mark.parametrize("tamper", [{}, {"head_sha": "f" * 40}, {"head_branch": "other"}, {"event": "push"}, {"run_attempt": 2}, {"display_title": "other trial"}])
def test_deadline_cancels_only_exact_known_dispatch_and_never_another_trial(backend, monkeypatch, tamper):
    backend._supports_dispatch_id = True
    clock, dispatched, calls = [0.0], {}, []
    monkeypatch.setattr("buildtrace.experiments.backend.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("buildtrace.experiments.backend.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def own_run():
        return {"id": 102, "head_sha": ORACLE, "head_branch": "main", "event": "workflow_dispatch", "display_title": f"BuildTrace oracle {CANDIDATE} {dispatched['inputs']['dispatch_id']}", "status": "in_progress", "run_attempt": 1}

    def api(method, path, payload=None):
        calls.append((method, path))
        if path.endswith("/dispatches"):
            dispatched.update(payload)
            return {}
        if path == "/actions/runs/102":
            return own_run() | tamper
        if path.endswith("/cancel"):
            return {}
        unrelated = {"id": 999, "head_sha": ORACLE, "display_title": "another trial", "status": "in_progress"}
        return {"workflow_runs": [unrelated] + ([own_run()] if dispatched else [])}

    backend._api = api
    with pytest.raises(BackendError, match="deadline") as error:
        backend._dispatch_and_capture("main", ORACLE, {"source_sha": CANDIDATE, "oracle_mode": True}, 2, archive_oracle=True)
    cancellation = error.value.diagnostics["cancellation"]
    assert cancellation["status"] == ("identity-not-confirmed" if tamper else "requested")
    posts = [path for method, path in calls if method == "POST"]
    assert posts.count("/actions/runs/102/cancel") == (0 if tamper else 1)
    assert not any("999/cancel" in path for _, path in calls)
    state = next(iter(backend._dispatches.values()))
    assert backend._cancel_overdue_dispatch(state) == cancellation
    assert [path for method, path in calls if method == "POST"] == posts


def test_legacy_shared_oracle_run_without_unique_dispatch_is_not_cancelled(backend):
    backend._api = lambda *args: pytest.fail("Unattributed shared-branch runs must not be touched")
    state = {"runs": [{"id": 102}], "dispatch_id": None, "ref": "main"}
    assert backend._cancel_overdue_dispatch(state) == {"status": "not-attributable"}


def test_poll_failure_at_deadline_still_cancels_only_known_run_once(backend, monkeypatch):
    backend._supports_dispatch_id = True
    clock, dispatched, polls, cancels = [0.0], {}, [], []
    monkeypatch.setattr("buildtrace.experiments.backend.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("buildtrace.experiments.backend.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def own_run():
        return {"id": 102, "head_sha": ORACLE, "head_branch": "main", "event": "workflow_dispatch", "display_title": f"BuildTrace oracle {CANDIDATE} {dispatched['inputs']['dispatch_id']}", "status": "in_progress", "run_attempt": 1}

    def api(method, path, payload=None):
        if path.endswith("/dispatches"):
            dispatched.update(payload)
            return {}
        if path == "/actions/runs/102":
            return own_run()
        if path.endswith("/cancel"):
            cancels.append(path)
            raise BackendError("Cancellation acknowledgement lost", diagnostics={"ambiguous_mutation": True})
        if not dispatched:
            return {"workflow_runs": []}
        polls.append(path)
        if len(polls) == 2:
            clock[0] = 10
            raise BackendError("Polling transport deadline")
        return {"workflow_runs": [own_run()]}

    backend._api = api
    with pytest.raises(BackendError, match="Polling transport deadline") as caught:
        backend._dispatch_and_capture("main", ORACLE, {"source_sha": CANDIDATE, "oracle_mode": True}, 10, archive_oracle=True)
    assert caught.value.diagnostics["cancellation"]["status"] == "request-failed"
    state = next(iter(backend._dispatches.values()))
    backend._cancel_overdue_dispatch(state)
    assert cancels == ["/actions/runs/102/cancel"]
