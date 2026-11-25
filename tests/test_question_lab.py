import json
import re
import time
from pathlib import Path

from buildtrace.experiments.runner import trial_id
from buildtrace.experiments.models import AgentMode
from buildtrace.web import create_app


def test_question_ui_requires_local_token_and_shows_real_trace(tmp_path, monkeypatch):
    source = Path("data/research-v2/experiments/dev-v2/experiment.json")
    destination = tmp_path / "research-v2" / "experiments" / "dev-v2" / "experiment.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    monkeypatch.setattr("buildtrace.question_lab.validate_local_inputs", lambda *_args, **_kwargs: {
        "local_prerequisites_ready": True, "remaining_inputs": [],
    })
    monkeypatch.setattr("buildtrace.question_lab.resolve_github_token", lambda: "test-token")

    def fake_runner(spec, _store, output, **kwargs):
        assert kwargs["allow_publish"] and kwargs["continue_on_infra"] and kwargs["split"] == "dev"
        assert spec.trials == 3 and spec.execution_parallelism == 3
        assert spec.retrieval.max_insights == 8
        assert callable(kwargs["backend_factory"])
        output.mkdir(parents=True)
        (output / "experiment.json").write_text(spec.model_dump_json() + "\n", encoding="utf-8")
        (output / "packets.json").write_text(json.dumps({spec.tasks[0].id: {
            "insight_ids": ["insight-0049"], "sha256": "a" * 64, "text": "<insight>packaged data</insight>",
        }}) + "\n", encoding="utf-8")
        identifier = trial_id(spec, spec.tasks[0], AgentMode.WIKITRACE, 0, "reference")
        directory = output / "trials" / identifier
        directory.mkdir(parents=True)
        rows = [
            {"type": "message", "role": "user", "content": "Current failure\n<memory_documents>\n<insight>packaged data</insight>\n</memory_documents>"},
            {"type": "observable_action", "kind": "plan", "decision": {"action": "investigate", "target": "pyproject.toml", "summary": "Check sdist"}},
            {"type": "function_call", "name": "read_historical_evidence", "arguments": {
                "insight_id": "insight-0049", "layer": "raw", "question": "Was the resource packaged?", "trigger": "missing-detail",
            }, "result": {"text": "original file observation", "citation": {
                "document": "raw/step-0024/solver.full.jsonl", "first_line": 3, "last_line": 3,
                "document_sha256": "b" * 64,
            }}, "is_error": False},
        ]
        (directory / "trajectory.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        (directory / "result.json").write_text(json.dumps({
            "outcome": "solved", "stop_reason": "verified", "verifications": [], "evidence_decisions": [],
            "packet_insight_ids": ["insight-0049"],
        }) + "\n", encoding="utf-8")
        return []

    app = create_app({"TESTING": True, "DATA_DIR": tmp_path, "ENABLE_QUESTION_RUNS": True})
    app.extensions["question_lab"].runner = fake_runner
    browser = app.test_client()
    page = browser.get("/questions")
    assert page.status_code == 200 and b"Resource missing from sdist" in page.data
    token = re.search(rb'name="token" value="([^"]+)"', page.data).group(1).decode()
    assert browser.post("/questions/p2/start", data={"token": "wrong"}).status_code == 403
    response = browser.post("/questions/p2/start", data={"token": token})
    assert response.status_code == 303
    run_url = response.headers["Location"]
    job_id = run_url.rsplit("/", 1)[-1]
    for _ in range(50):
        if app.extensions["question_lab"].get_job(job_id)["status"] == "completed":
            break
        time.sleep(0.02)
    state = browser.get(f"/api/questions/runs/{job_id}").get_json()
    assert state["job"]["status"] == "completed"
    assert len(state["trials"]) == 9 and state["completed"] == 1
    assert state["packet"]["insight_ids"] == ["insight-0049"]
    assert next(row for row in state["trials"] if row["status"] == "solved")["packet_delivered"] is True
    assert all(not row["packet_delivered"] for row in state["trials"] if row["mode"] == "no-memory")
    assert browser.get(run_url).status_code == 200
    trial = next(row for row in state["trials"] if row["status"] == "solved")
    detail = browser.get(f"{run_url}/trials/{trial['id']}")
    assert detail.status_code == 200
    assert b"original file observation" in detail.data and b"raw source delivered" in detail.data
    assert browser.get(f"{run_url}/trials/other").status_code == 404


def test_question_launch_disabled_by_default(tmp_path):
    browser = create_app({"TESTING": True, "DATA_DIR": tmp_path}).test_client()
    page = browser.get("/questions")
    assert b"--enable-question-runs" in page.data
    token = re.search(rb'name="token" value="([^"]+)"', page.data).group(1).decode()
    assert browser.post("/questions/p2/start", data={"token": token}).status_code == 400
