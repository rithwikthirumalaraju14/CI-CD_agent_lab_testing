import base64
import io
import zipfile

import httpx
import pytest

from buildtrace.github import GitHubActionsClient
from buildtrace.models import ApiDocument, CollectedRun, RunReference, SourceFile

RUN_URL = "https://github.com/example/invoice/actions/runs/12345"
SHA = "b" * 40


def log_zip(files=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (files or {"invoice-checks/8_Run tests.txt": b"FAILED test_total\n5 failed, 31 passed\n"}).items():
            archive.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def collected_run():
    return CollectedRun(
        reference=RunReference.parse(RUN_URL + "/attempts/2"),
        run={
            "id": 12345,
            "run_attempt": 2,
            "name": "Invoice CI",
            "head_sha": SHA,
            "event": "push",
            "status": "completed",
            "conclusion": "failure",
            "path": ".github/workflows/ci.yml",
        },
        jobs=[
            {
                "id": 71,
                "name": "invoice-checks",
                "conclusion": "failure",
                "steps": [{"name": "Run tests", "number": 8, "conclusion": "failure"}],
            }
        ],
        source_ref=SHA,
        sources=[SourceFile("pyproject.toml", b'[project]\nname = "invoice"\n')],
        api_documents=[ApiDocument("/repos/example/invoice/actions/runs/12345", b'{ "id": 12345 }\n')],
        log_archive=log_zip(),
    )


@pytest.fixture
def github_simulator(collected_run):
    """Represent GitHub's documented HTTP boundary, including a newer attempt."""
    requests = []

    def make_factory(*, log_status=302, redirect_host="logs.blob.core.windows.net", paginate=False, event="push", archive=None):
        def handler(request):
            requests.append(request)
            path = request.url.path
            if request.url.host == redirect_host and path == "/download":
                return httpx.Response(200, content=archive if archive is not None else log_zip())
            if path == "/repos/example/invoice/actions/runs/12345":
                return httpx.Response(200, json=collected_run.run | {"run_attempt": 3})
            if path in {
                "/repos/example/invoice/actions/runs/12345/attempts/2",
                "/repos/example/invoice/actions/runs/12345/attempts/3",
            }:
                attempt = int(path.rsplit("/", 1)[-1])
                return httpx.Response(200, json=collected_run.run | {"run_attempt": attempt, "event": event})
            if path.endswith("/jobs"):
                page = request.url.params.get("page")
                if paginate and page == "1":
                    return httpx.Response(200, json={"jobs": collected_run.jobs * 100, "total_count": 101})
                return httpx.Response(200, json={"jobs": collected_run.jobs, "total_count": 1})
            if path.endswith("/logs"):
                if log_status == 302:
                    return httpx.Response(302, headers={"Location": f"https://{redirect_host}/download?signed=example"})
                return httpx.Response(log_status)
            if path.endswith("/artifacts"):
                return httpx.Response(200, json={"artifacts": []})
            if "/contents/" in path:
                relative = path.split("/contents/", 1)[1]
                sources = {
                    "pyproject.toml": b'[project]\nname = "invoice"\n',
                    ".github/workflows/ci.yml": b"name: Invoice CI\non: push\n",
                }
                if relative in sources:
                    content = sources[relative]
                    return httpx.Response(
                        200,
                        json={"type": "file", "encoding": "base64", "content": base64.b64encode(content).decode(), "size": len(content)},
                    )
            return httpx.Response(404)

        def factory():
            client = httpx.Client(
                transport=httpx.MockTransport(handler),
                headers={"Authorization": "Bearer default-client-token"},
            )
            return GitHubActionsClient(token="test-token", client=client)

        return factory

    return make_factory, requests
