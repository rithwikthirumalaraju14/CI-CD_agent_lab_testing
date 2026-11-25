import httpx
import pytest

from buildtrace.github import GitHubActionsClient, GitHubError
from buildtrace.models import RunReference

from conftest import RUN_URL, SHA


def test_selected_attempt_pagination_sources_and_credential_boundary(github_simulator):
    make_factory, requests = github_simulator
    with make_factory(paginate=True)() as client:
        collected = client.collect(RunReference.parse(RUN_URL + "/attempts/2"))
    assert collected.reference.attempt == 2
    assert collected.run["run_attempt"] == 2
    assert len(collected.jobs) == 101
    assert collected.log_archive.startswith(b"PK")
    assert not collected.warnings
    assert {source.path for source in collected.sources} == {"pyproject.toml", ".github/workflows/ci.yml"}
    jobs = [request for request in requests if request.url.path.endswith("/jobs")]
    assert [request.url.params["page"] for request in jobs] == ["1", "2"]
    assert all("/attempts/2/" in request.url.path for request in jobs)
    sources = [request for request in requests if "/contents/" in request.url.path]
    assert all(request.url.params["ref"] == SHA for request in sources)
    storage = next(request for request in requests if request.url.host.endswith(".blob.core.windows.net"))
    assert "Authorization" not in storage.headers
    metadata = next(request for request in requests if request.url.host == "api.github.com")
    assert metadata.headers["Authorization"] == "Bearer test-token"
    assert collected.api_documents[0].content.startswith(b"{")


def test_default_capture_resolves_then_pins_latest_attempt(github_simulator):
    make_factory, requests = github_simulator
    with make_factory()() as client:
        collected = client.collect(RunReference.parse(RUN_URL))
    assert collected.reference.attempt == 3
    assert collected.run["run_attempt"] == 3
    assert any(request.url.path.endswith("/attempts/3/logs") for request in requests)


def test_expired_logs_preserve_other_evidence_and_a_warning(github_simulator):
    make_factory, _ = github_simulator
    with make_factory(log_status=410)() as client:
        collected = client.collect(RunReference.parse(RUN_URL))
    assert collected.log_archive is None
    assert collected.jobs
    assert collected.sources
    assert any("expired" in warning for warning in collected.warnings)


def test_untrusted_download_is_not_followed(github_simulator):
    make_factory, requests = github_simulator
    with make_factory(redirect_host="attacker.test")() as client:
        collected = client.collect(RunReference.parse(RUN_URL))
    assert collected.log_archive is None
    assert any("unsupported log download location" in warning for warning in collected.warnings)
    assert all(request.url.host != "attacker.test" for request in requests)


def test_oversized_download_is_reported(github_simulator, monkeypatch):
    monkeypatch.setattr("buildtrace.github.MAX_ARCHIVE_BYTES", 10)
    make_factory, _ = github_simulator
    with make_factory()() as client:
        collected = client.collect(RunReference.parse(RUN_URL))
    assert collected.log_archive is None
    assert any("download limit" in warning for warning in collected.warnings)


def test_pull_request_source_reference_is_explicit(github_simulator):
    make_factory, _ = github_simulator
    with make_factory(event="pull_request")() as client:
        collected = client.collect(RunReference.parse(RUN_URL))
    assert collected.source_ref == SHA
    assert any("merge revision" in warning for warning in collected.warnings)


def test_rate_limit_has_actionable_error():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
    )
    with GitHubActionsClient(token="test-token", client=httpx.Client(transport=transport)) as client:
        with pytest.raises(GitHubError, match="rate limit") as error:
            client.collect(RunReference.parse(RUN_URL))
    assert error.value.status_code == 403
    assert "test-token" not in str(error.value)


def test_connection_failure_is_actionable():
    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with GitHubActionsClient(token="test-token", client=httpx.Client(transport=httpx.MockTransport(timeout))) as client:
        with pytest.raises(GitHubError, match="Could not reach GitHub"):
            client.collect(RunReference.parse(RUN_URL))


def test_mismatched_attempt_is_rejected(collected_run):
    def handler(request):
        return httpx.Response(200, json=collected_run.run | {"run_attempt": 3})

    with GitHubActionsClient(token="test-token", client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        with pytest.raises(GitHubError, match="different workflow run attempt"):
            client.collect(RunReference.parse(RUN_URL + "/attempts/2"))
