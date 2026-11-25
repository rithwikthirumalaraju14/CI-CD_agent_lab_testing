import errno
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.backend import BackendError, GitHubTrialBackend
from buildtrace.github import GetRetryPolicy, GitHubActionsClient, GitHubError

from experiment_fixtures import experiment_spec


NO_WAIT = GetRetryPolicy(base_delay_seconds=0, max_delay_seconds=0, max_wait_seconds=0)


def test_staging_lock_retries_publish_only_with_identical_bytes(tmp_path, collected_run, monkeypatch):
    store = EvidenceStore(tmp_path)
    rename = Path.rename
    attempts, sleeps = [], []

    def locked(source, destination):
        assert source.name.startswith(".pending-")
        assert not destination.exists()
        attempts.append((source, destination, {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}))
        if len(attempts) < 3:
            raise PermissionError(errno.EACCES, "simulated Windows scanner lock")
        return rename(source, destination)

    monkeypatch.setattr(Path, "rename", locked)
    monkeypatch.setattr("buildtrace.evidence.time.sleep", sleeps.append)
    manifest = store.save(collected_run)
    assert len(attempts) == 3 and attempts[0] == attempts[1] == attempts[2]
    assert sleeps == [0.05, 0.1]
    assert list(store.directory.iterdir()) == [store.capture_path(manifest["id"])]
    assert store.verify(manifest["id"])["integrity"] == "verified"


def test_permanent_publish_lock_is_bounded_and_leaves_prior_capture_immutable(tmp_path, collected_run, monkeypatch):
    store = EvidenceStore(tmp_path)
    prior = store.save(collected_run)
    attempts, sleeps = [], []

    def locked(source, destination):
        attempts.append((source, destination))
        raise PermissionError(errno.EACCES, "persistent lock")

    monkeypatch.setattr(Path, "rename", locked)
    monkeypatch.setattr("buildtrace.evidence.time.sleep", sleeps.append)
    with pytest.raises(PermissionError):
        store.save(collected_run)
    assert len(attempts) == 5 and sum(sleeps) == pytest.approx(0.75)
    assert list(store.directory.iterdir()) == [store.capture_path(prior["id"])]
    assert store.load(prior["id"]) == prior
    assert store.verify(prior["id"])["integrity"] == "verified"


def test_publish_does_not_retry_other_io_errors_or_replace_existing_capture(tmp_path, collected_run, monkeypatch):
    store = EvidenceStore(tmp_path)
    prior = store.save(collected_run)
    attempts = []

    def broken(source, destination):
        attempts.append(source)
        raise OSError(errno.EIO, "disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", broken)
        patch.setattr("buildtrace.evidence.time.sleep", lambda _: pytest.fail("Non-lock errors must not retry"))
        with pytest.raises(OSError, match="disk failure"):
            store.save(collected_run)
    assert len(attempts) == 1
    monkeypatch.setattr("buildtrace.evidence.uuid.uuid4", lambda: SimpleNamespace(hex=prior["id"]))
    with pytest.raises(FileExistsError):
        store.save(collected_run)
    assert store.load(prior["id"]) == prior
    assert store.verify(prior["id"])["integrity"] == "verified"


def test_json_get_retries_timeout_429_and_5xx_without_duplicate_evidence():
    calls = []
    raw = b'{ "id": 7, "raw": true }\n'

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("interrupted", request=request)
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if len(calls) == 3:
            return httpx.Response(503)
        return httpx.Response(200, content=raw)

    policy = GetRetryPolicy(max_attempts=4, base_delay_seconds=0, max_delay_seconds=0, max_wait_seconds=0)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with GitHubActionsClient(token="offline-token", client=http, retry_policy=policy) as client:
            assert client._get_json("/test") == {"id": 7, "raw": True}
            assert len(client._documents) == 1 and client._documents[0].content == raw
    assert len(calls) == 4 and all(request.method == "GET" for request in calls)


@pytest.mark.parametrize("status,content", [
    (401, b""), (403, b""), (404, b""), (409, b""), (410, b""), (422, b""),
    (200, b"not JSON"), (200, b"[]"),
])
def test_permanent_get_failures_are_not_retried(status, content):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=content)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with GitHubActionsClient(token="offline-token", client=http, retry_policy=NO_WAIT) as client:
            with pytest.raises(GitHubError):
                client._get_json("/test")
            assert client._documents == []
    assert len(calls) == 1


def test_get_wait_and_deadline_budgets_do_not_retry_before_retry_after(monkeypatch):
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    monkeypatch.setattr("buildtrace.github.time.sleep", sleeps.append)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with GitHubActionsClient(token="offline-token", client=http) as client:
            with pytest.raises(GitHubError) as error:
                client._get_json("/test")
            assert error.value.status_code == 429
    assert len(calls) == 1 and sleeps == []

    calls.clear()
    monkeypatch.setattr("buildtrace.github.time.monotonic", lambda: 100)
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(503))) as http:
        with GitHubActionsClient(token="offline-token", client=http, deadline=100.1) as client:
            with pytest.raises(GitHubError):
                client._get_json("/test")
    assert len(calls) == 1 and sleeps == []


def test_exhausted_get_retries_keep_status_and_attempt_count():
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(502))) as http:
        with GitHubActionsClient(token="offline-token", client=http, retry_policy=NO_WAIT) as client:
            with pytest.raises(GitHubError) as error:
                client._get_json("/test")
    assert len(calls) == error.value.retry_attempts == 3
    assert error.value.status_code == 502


def test_interrupted_signed_download_restarts_from_api_without_partial_bytes_or_credentials():
    calls, closed = [], []

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial-not-evidence"
            raise httpx.ReadTimeout("interrupted body")

        def close(self):
            closed.append(True)

    def handler(request):
        calls.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"Location": "https://logs.blob.core.windows.net/download?signed=offline"})
        storage_calls = [call for call in calls if call.url.host != "api.github.com"]
        if len(storage_calls) == 1:
            return httpx.Response(200, stream=Interrupted())
        if len(storage_calls) == 2:
            return httpx.Response(500)
        return httpx.Response(200, content=b"complete-original-zip-bytes")

    with httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer client-default"}) as http:
        with GitHubActionsClient(token="offline-token", client=http, retry_policy=NO_WAIT) as client:
            assert client._download_logs("/test/logs") == b"complete-original-zip-bytes"
    assert closed == [True]
    assert len(calls) == 6
    assert all("Authorization" not in request.headers for request in calls if request.url.host != "api.github.com")
    assert all(request.headers["Authorization"] == "Bearer offline-token" for request in calls if request.url.host == "api.github.com")


@pytest.mark.parametrize("failure", ["timeout", 429, 503])
def test_backend_never_retries_mutating_requests(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("lost acknowledgement", request=request)
        return httpx.Response(failure)

    backend = GitHubTrialBackend.__new__(GitHubTrialBackend)
    backend.base, backend.headers, backend.retry_policy = "/repos/example/invoice", {}, NO_WAIT
    with httpx.Client(transport=httpx.MockTransport(handler)) as backend.client:
        with pytest.raises(BackendError) as error:
            backend._api("POST", "/actions/workflows/ci.yml/dispatches", {"ref": "main"})
    assert len(calls) == 1
    assert error.value.diagnostics["request_attempts"] == 1
    assert error.value.diagnostics["ambiguous_mutation"] == (failure != 429)


def test_backend_source_get_retries_but_directory_and_bad_paths_remain_tool_errors():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"type": "file", "name": "constraints.txt"}])

    backend = GitHubTrialBackend.__new__(GitHubTrialBackend)
    backend.base, backend.headers, backend.retry_policy = "/repos/example/invoice", {}, NO_WAIT
    backend.task, backend.originals, backend.overlay = experiment_spec().tasks[0], {}, {}
    with httpx.Client(transport=httpx.MockTransport(handler)) as backend.client:
        with pytest.raises(ValueError, match="directory.*list_current_files"):
            backend.read_file("ci")
        for path in (None, 7, ".", "../outside", ".git/config"):
            with pytest.raises(ValueError):
                backend.read_file(path)
    assert len(calls) == 3 and backend.originals == {}


@pytest.mark.parametrize("body", [None, {"type": "file", "encoding": "base64", "content": None}, {"unexpected": "object"}])
def test_invalid_source_api_responses_are_infrastructure_errors_not_tool_mistakes(body):
    calls = []
    backend = GitHubTrialBackend.__new__(GitHubTrialBackend)
    backend.base, backend.headers, backend.retry_policy = "/repos/example/invoice", {}, NO_WAIT
    backend.task, backend.originals, backend.overlay = experiment_spec().tasks[0], {}, {}
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, content=json.dumps(body)))) as backend.client:
        with pytest.raises(BackendError):
            backend.read_file("src/bench_app/main.py")
    assert len(calls) == 1 and backend.originals == {}
