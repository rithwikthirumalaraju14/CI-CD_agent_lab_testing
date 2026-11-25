import json

from buildtrace.dashboard import _recorded_cost, load_study
from buildtrace.web import create_app


def test_dashboard_uses_complete_trials_strict_gain_and_locked_review(tmp_path):
    directory = tmp_path / "exploratory-25" / "experiments" / "round-1"
    directory.mkdir(parents=True)
    (directory / "experiment.json").write_text(json.dumps({
        "id": "exploratory-test", "trials": 3,
        "model": {"model": "gpt-6-luna"},
        "tasks": [{"id": "case-001", "family": "example", "split": "dev"}],
    }), encoding="utf-8")
    records = []
    for trial in range(3):
        for mode in ("no-memory", "insights-only", "wikitrace"):
            records.append({
                "id": f"run-{mode}-{trial}", "task_id": "case-001", "mode": mode, "trial": trial,
                "role": "reference", "outcome": "solved" if mode == "wikitrace" or (mode == "insights-only" and trial != 1) else "unsolved",
                "model": {"model": "gpt-6-luna"}, "returned_models": ["gpt-6-luna"],
                "input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 100, "reasoning_tokens": 30,
                "timing": {"wall_seconds": 30, "ci_wait_seconds": 10, "approval_wait_seconds": 0},
                "model_calls": 2, "patch_attempts": 1, "evidence_reads": [], "evidence_decisions": [],
                "hunks": [{"classification": "unreviewed"}], "verifications": [],
                "trajectory_path": "trials/not-present/trajectory.jsonl",
            })
    (directory / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    trace = directory / "trials" / "not-present" / "trajectory.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_bytes(b'{"type":"tool"}\n')
    reviewed_dir = tmp_path / "exploratory-25" / "analysis" / "round-1"
    reviewed_dir.mkdir(parents=True)
    reviewed = [dict(r, hunks=[{"classification": "unnecessary"}] if r["mode"] == "wikitrace" and r["trial"] == 0 else r["hunks"]) for r in records]
    (reviewed_dir / "fully-reviewed.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reviewed), encoding="utf-8")

    data = load_study(tmp_path, "exploratory-25/round-1")
    assert (data["planned_trials"], data["completed_trials"]) == (9, 9)
    assert data["modes"]["wikitrace"]["gain"] == {"numerator": 3, "denominator": 3, "percent": 100.0}
    assert data["modes"]["insights-only"]["gain"] == {"numerator": 2, "denominator": 3, "percent": 66.7}
    assert data["modes"]["wikitrace"]["unnecessary_hunks"] == 1
    assert data["modes"]["wikitrace"]["unreviewed_hunks"] == 2
    assert data["modes"]["wikitrace"]["tokens"]["billable_tokens"] == 3900
    assert data["modes"]["wikitrace"]["tokens"]["reasoning_tokens"] == 90
    assert data["pairs"]["no-memory"]["wikitrace_win"] == 3
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    browser = app.test_client()
    page = browser.get("/dashboard?study=exploratory-25/round-1")
    assert page.status_code == 200 and b"9 / 9 trials recorded" in page.data
    assert browser.get("/api/dashboard?study=exploratory-25/round-1").get_json()["completed_trials"] == 9
    assert browser.get("/dashboard.csv?study=exploratory-25/round-1").data.count(b"case-001") == 9
    assert browser.get("/dashboard/download/results?study=exploratory-25/round-1").data.count(b"case-001") == 9
    assert browser.get("/dashboard/download/experiment?study=exploratory-25/round-1").status_code == 200
    assert browser.get("/dashboard/trajectory/run-wikitrace-0?study=exploratory-25/round-1").data == b'{"type":"tool"}\n'
    assert browser.get("/dashboard/trajectory/no-such-trial?study=exploratory-25/round-1").status_code == 404
    assert browser.get("/dashboard/download/unknown?study=exploratory-25/round-1").status_code == 404
    assert browser.get("/dashboard?study=../../private").status_code == 404


def test_cost_uses_per_response_long_context_and_does_not_double_count_reasoning(tmp_path):
    directory = tmp_path / "study"
    journal = directory / "trials" / "run-1" / "trajectory.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"type": "message", "usage": {
        "input_tokens": 300_000, "cached_input_tokens": 100_000,
        "output_tokens": 1_000, "reasoning_tokens": 400,
    }}) + "\n", encoding="utf-8")
    record = {"model": {"model": "gpt-6-luna"}, "returned_models": ["gpt-6-luna"],
              "input_tokens": 300_000, "cached_input_tokens": 100_000,
              "output_tokens": 1_000, "reasoning_tokens": 400,
              "trajectory_path": "trials/run-1/trajectory.jsonl"}
    assert _recorded_cost(record, directory) == (0.04275, "per-response")
    assert _recorded_cost({"model": {"model": "gpt-6-luna"}, "model_calls": 1,
                           "input_tokens": 0, "output_tokens": 0}, directory) == (None, "missing-usage")
