import json

from buildtrace import cli
from buildtrace.evidence import EvidenceStore
from buildtrace.service import capture_run

from conftest import RUN_URL


def test_inspect_reports_ci_failure_but_successful_capture(tmp_path, github_simulator, monkeypatch, capsys):
    make_factory, _ = github_simulator
    monkeypatch.setattr(cli, "capture_run", lambda url, store: capture_run(url, store, make_factory()))
    assert cli.main(["inspect", RUN_URL, "--data-dir", str(tmp_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["conclusion"] == "failure"
    assert output["failed_jobs"] == ["invoice-checks"]
    assert output["evidence_files"] > 0
    assert EvidenceStore(tmp_path).load(output["capture_id"])["reference"]["attempt"] == 3


def test_verify_command(tmp_path, collected_run, capsys):
    manifest = EvidenceStore(tmp_path).save(collected_run)
    assert cli.main(["verify", manifest["id"], "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["integrity"] == "verified"


def test_invalid_url_returns_an_error_without_creating_capture(tmp_path, capsys):
    assert cli.main(["inspect", "http://localhost", "--data-dir", str(tmp_path)]) == 2
    assert "GitHub Actions run URL" in capsys.readouterr().err
    assert EvidenceStore(tmp_path).recent() == []
