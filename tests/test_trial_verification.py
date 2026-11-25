import io
import time
import zipfile

import httpx
import pytest

from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.backend import BackendError, GitHubTrialBackend, changed_hunks, junit_passed, required_checks_passed, validate_workflow_environment_change

from experiment_fixtures import experiment_spec


def junit_archive(tests=36, failures=0, skipped=0):
    xml = f'<testsuites><testsuite tests="{tests}" failures="{failures}" errors="0" skipped="{skipped}">'
    xml += "".join(f'<testcase name="test_{index}"/>' for index in range(tests))
    xml += "</testsuite></testsuites>"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("artifacts/pytest.xml", xml)
    return stream.getvalue()


def test_oracle_rejects_partial_or_skipped_suites():
    assert junit_passed(junit_archive(), 36)
    assert not junit_passed(junit_archive(tests=35), 36)
    assert not junit_passed(junit_archive(failures=1), 36)
    assert not junit_passed(junit_archive(skipped=1), 36)
    assert not junit_passed(b"not a zip", 36)


def test_green_job_with_skipped_required_step_is_not_a_fix():
    task = experiment_spec().tasks[0]
    capture = {"run": {"conclusion": "success"}, "jobs": [{"name": "invoice-checks", "conclusion": "success", "steps": [{"name": name, "conclusion": "success"} for name in task.required_steps["invoice-checks"]]}]}
    assert required_checks_passed(task, capture)
    capture["jobs"][0]["steps"][0]["conclusion"] = "skipped"
    assert not required_checks_passed(task, capture)


def test_edit_guards_and_hunk_units():
    backend = GitHubTrialBackend.__new__(GitHubTrialBackend)
    backend.task = experiment_spec().tasks[0]
    backend.overlay = {}
    backend.originals = {"src/invoice_lab/calculator.py": "total = subtotal + tax\n"}
    changes = [{"path": "src/invoice_lab/calculator.py", "content": "total = taxable_amount + tax\n"}]
    overlay = backend._check_changes(changes)
    units = changed_hunks(backend.originals, overlay)
    assert len(units) == 1 and units[0].added_lines == units[0].deleted_lines == 1
    assert units[0].classification == "unreviewed"
    for path in ("tests/test_calculator.py", ".github/workflows/ci.yml", "../outside.py"):
        with pytest.raises(ValueError):
            backend._check_changes([{"path": path, "content": "disabled"}])


def test_configuration_fix_cannot_replace_checks_with_echo_or_disable_tests():
    before = "on: push\njobs:\n  check:\n    env:\n      INVOICE_CURRENCY: INVALID\n    steps:\n      - run: python -m pytest tests\n"
    valid = before.replace("INVALID", "USD")
    validate_workflow_environment_change(before, valid, ["INVOICE_CURRENCY"])
    with pytest.raises(ValueError, match="not commands"):
        validate_workflow_environment_change(before, valid.replace("python -m pytest tests", "echo success"), ["INVOICE_CURRENCY"])
    with pytest.raises(ValueError, match="PYTEST_ADDOPTS"):
        validate_workflow_environment_change(before, valid.replace("INVOICE_CURRENCY: USD", 'INVOICE_CURRENCY: USD\n      PYTEST_ADDOPTS: "--deselect=test_total"'), ["INVOICE_CURRENCY"])


def test_directory_response_is_a_recoverable_tool_error():
    backend = GitHubTrialBackend.__new__(GitHubTrialBackend)
    backend.task = experiment_spec().tasks[0]
    backend.base, backend.headers = "/repos/example/invoice", {}
    backend.originals, backend.overlay = {}, {}
    backend.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[{"name": "constraints.txt", "type": "file"}])
    ))
    with pytest.raises(ValueError, match="directory.*list_current_files"):
        backend.read_file("ci")
    assert "ci" not in backend.originals
    backend.client.close()


def test_source_network_failure_is_recordable_infrastructure_error():
    def failure(request):
        raise httpx.ReadTimeout("simulated network interruption", request=request)

    backend = GitHubTrialBackend.__new__(GitHubTrialBackend)
    backend.task = experiment_spec().tasks[0]
    backend.base, backend.headers = "/repos/example/invoice", {}
    backend.originals, backend.overlay = {}, {}
    backend.client = httpx.Client(transport=httpx.MockTransport(failure))
    with pytest.raises(BackendError, match="fixed task snapshot"):
        backend.read_file("src/invoice_lab/calculator.py")
    backend.client.close()


def test_oracle_dispatch_does_not_reuse_an_old_or_unrelated_run(tmp_path, collected_run, monkeypatch):
    backend = GitHubTrialBackend.__new__(GitHubTrialBackend)
    backend.task = experiment_spec().tasks[0]
    backend.base = "/repos/example/invoice"
    backend.token = "test"
    backend.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    backend.store = EvidenceStore(tmp_path)
    backend.allowed_captures = set()
    state = {"dispatched": False}
    head = "c" * 40

    def run(identifier, title):
        return {"id": identifier, "head_sha": head, "display_title": title, "status": "completed", "html_url": f"https://github.com/example/invoice/actions/runs/{identifier}", "run_attempt": 1}

    def api(method, path, payload=None):
        if method == "POST":
            state["dispatched"] = True
            return {}
        rows = [run(100, "Oracle desired")]
        if state["dispatched"]:
            rows += [run(101, "Oracle another-candidate"), run(102, "Oracle desired")]
        return {"workflow_runs": rows}

    observed = []

    def collect(client, reference):
        observed.append(reference.run_id)
        return collected_run

    backend._api = api
    monkeypatch.setattr("buildtrace.experiments.backend.GitHubActionsClient.collect", collect)
    backend._dispatch_and_capture("main", head, {}, time.monotonic() + 30, expected_title="Oracle desired")
    assert observed == [102]
