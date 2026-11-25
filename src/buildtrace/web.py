"""A small, server-rendered browser for archived Actions evidence."""

import io
import os
import re
import csv
import hmac
import secrets
from collections.abc import Callable
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, url_for

from buildtrace import __version__
from buildtrace.evidence import EvidenceError, EvidenceStore
from buildtrace.dashboard import list_studies, load_study
from buildtrace.github import GitHubActionsClient, GitHubError
from buildtrace.models import FAILED_CONCLUSIONS
from buildtrace.question_lab import QUESTION_BY_ID, QUESTIONS, QuestionLab
from buildtrace.service import capture_run

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def create_app(
    config: dict | None = None,
    *,
    github_factory: Callable[[], GitHubActionsClient] = GitHubActionsClient,
) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATA_DIR=os.environ.get("BUILDTRACE_DATA_DIR", "data"),
        MAX_CONTENT_LENGTH=8 * 1024,
        ENABLE_QUESTION_RUNS=False,
    )
    if config:
        app.config.update(config)
    store = EvidenceStore(app.config["DATA_DIR"])
    app.extensions["evidence_store"] = store
    question_lab = QuestionLab(Path(app.config["DATA_DIR"]), enabled=app.config["ENABLE_QUESTION_RUNS"])
    app.extensions["question_lab"] = question_lab
    launch_token = secrets.token_urlsafe(32)

    @app.get("/questions")
    def questions_page():
        return render_template("questions.html", questions=QUESTIONS, lab=question_lab,
                               recent=question_lab.jobs(), launch_token=launch_token, error=None)

    @app.post("/questions/<question_id>/start")
    def start_question(question_id: str):
        if question_id not in QUESTION_BY_ID:
            abort(404)
        if request.remote_addr not in {"127.0.0.1", "::1"} or not hmac.compare_digest(request.form.get("token", ""), launch_token):
            abort(403)
        try:
            job = question_lab.start(question_id)
        except ValueError as exc:
            return render_template("questions.html", questions=QUESTIONS, lab=question_lab,
                                   recent=question_lab.jobs(), launch_token=launch_token, error=str(exc)), 400
        return redirect(url_for("question_run", job_id=job["id"]), code=303)

    @app.get("/questions/runs/<job_id>")
    def question_run(job_id: str):
        try:
            state = question_lab.state(job_id)
        except KeyError:
            abort(404)
        return render_template("question_run.html", **state)

    @app.get("/api/questions/runs/<job_id>")
    def question_run_state(job_id: str):
        try:
            state = question_lab.state(job_id)
        except KeyError:
            abort(404)
        question = state.pop("question")
        return jsonify(state | {"question": {"id": question.id, "title": question.title, "case_id": question.case_id}})

    @app.get("/questions/runs/<job_id>/trials/<trial_id>")
    def question_trial(job_id: str, trial_id: str):
        try:
            trace = question_lab.trace(job_id, trial_id)
        except KeyError:
            abort(404)
        return render_template("question_trial.html", **trace)

    @app.route("/", methods=["GET", "POST"])
    def index():
        error = None
        status = 200
        run_url = ""
        if request.method == "POST":
            run_url = request.form.get("run_url", "").strip()
            try:
                manifest = capture_run(run_url, store, github_factory)
                return redirect(url_for("capture_detail", capture_id=manifest["id"]), code=303)
            except ValueError as exc:
                error, status = str(exc), 400
            except GitHubError as exc:
                error, status = str(exc), 502
            except OSError:
                error, status = "The evidence archive could not be saved. Check the data directory.", 500
        return render_template("index.html", recent=store.recent(), error=error, run_url=run_url), status

    @app.get("/captures/<capture_id>")
    def capture_detail(capture_id: str):
        manifest = store.load(capture_id)
        failed_jobs = [job for job in manifest["jobs"] if job.get("conclusion") in FAILED_CONCLUSIONS]
        failed_names = [
            re.sub(r"[^a-z0-9]", "", step.get("name", "").lower())
            for job in failed_jobs
            for step in job.get("steps", [])
            if step.get("conclusion") in FAILED_CONCLUSIONS and step.get("name")
        ]
        previews = []
        for entry in manifest["entries"]:
            if entry["kind"] not in {"source", "log"}:
                continue
            _, content = store.read_entry(capture_id, entry["id"])
            lines = _ANSI.sub("", content.decode("utf-8-sig", errors="replace")).splitlines()
            normalized = re.sub(r"[^a-z0-9]", "", entry["label"].lower())
            previews.append(
                entry
                | {
                    "preview": "\n".join(lines[:140]),
                    "line_count": len(lines),
                    "truncated": len(lines) > 140,
                    "failure_match": any(name in normalized for name in failed_names),
                }
            )
        logs = sorted(
            (entry for entry in previews if entry["kind"] == "log"),
            key=lambda entry: (not entry["failure_match"], entry["label"]),
        )
        return render_template(
            "capture.html",
            capture=manifest,
            failed_jobs=failed_jobs,
            logs=logs,
            sources=[entry for entry in previews if entry["kind"] == "source"],
            api_entries=[entry for entry in manifest["entries"] if entry["kind"] == "api"],
            archive=next((entry for entry in manifest["entries"] if entry["kind"] == "archive"), None),
        )

    @app.get("/captures/<capture_id>/files/<entry_id>")
    def evidence_file(capture_id: str, entry_id: str):
        entry, content = store.read_entry(capture_id, entry_id)
        kind = entry["kind"]
        binary_archive = kind == "archive" or entry["path"].endswith(".zip")
        mimetype = "application/zip" if binary_archive else "application/json" if kind == "api" else "text/plain"
        return send_file(
            io.BytesIO(content),
            mimetype=mimetype,
            as_attachment=binary_archive or request.args.get("download") == "1",
            download_name=Path(entry["path"]).name,
            max_age=0,
        )

    @app.get("/api/captures/<capture_id>")
    def capture_manifest(capture_id: str):
        return jsonify(store.load(capture_id))

    @app.get("/health")
    def health():
        return jsonify(status="ok", version=__version__)

    def selected_dashboard():
        studies = list_studies(Path(app.config["DATA_DIR"]))
        slug = request.args.get("study") or (studies[0]["slug"] if studies else None)
        if slug is None:
            return studies, None
        try:
            return studies, load_study(Path(app.config["DATA_DIR"]), slug)
        except KeyError:
            abort(404)

    @app.get("/dashboard")
    def dashboard_page():
        studies, dashboard = selected_dashboard()
        return render_template("dashboard.html", studies=studies, dashboard=dashboard)

    @app.get("/api/dashboard")
    def dashboard_data():
        _studies, dashboard = selected_dashboard()
        return jsonify(dashboard or {"studies": [], "message": "No recorded experiments found"})

    @app.get("/dashboard.csv")
    def dashboard_csv():
        _studies, dashboard = selected_dashboard()
        if dashboard is None:
            abort(404)
        stream = io.StringIO()
        fields = (
            "data_origin", "task_id", "mode", "trial", "outcome", "stop_reason", "wall_seconds", "ci_wait_seconds",
            "active_seconds", "model_calls", "patch_attempts", "input_tokens", "cached_input_tokens",
            "uncached_input_tokens", "output_tokens", "reasoning_tokens", "billable_tokens", "cost_usd",
            "history_requests", "history_deliveries", "raw_deliveries", "reviewed_adopted_effects",
            "unnecessary_hunks", "unreviewed_hunks", "ci_url", "oracle_url",
        )
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for trial in dashboard["trials"]:
            row = dict(trial)
            # CSV readers such as spreadsheets may interpret untrusted text as a formula.
            for field in ("stop_reason", "task_id"):
                value = row.get(field)
                if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
                    row[field] = "'" + value
            writer.writerow(row)
        return Response(stream.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{dashboard["slug"].replace("/", "-")}-trials.csv"'
        })

    @app.get("/dashboard/download/<kind>")
    def dashboard_download(kind: str):
        _studies, dashboard = selected_dashboard()
        if dashboard is None or kind not in {"experiment", "results", "manifest", "reviewed_results"}:
            abort(404)
        name = dashboard["source_files"].get(kind)
        if not name or not Path(name).is_file():
            abort(404)
        return send_file(name, as_attachment=True, download_name=Path(name).name, max_age=0)

    @app.get("/dashboard/trajectory/<trial_id>")
    def dashboard_trajectory(trial_id: str):
        _studies, dashboard = selected_dashboard()
        if dashboard is None:
            abort(404)
        row = next((item for item in dashboard["trials"] if item["id"] == trial_id), None)
        if row is None or not row["trajectory_available"]:
            abort(404)
        directory = Path(dashboard["source_files"]["experiment"]).parent.resolve()
        path = (directory / row["trajectory_path"]).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=f"{trial_id}-trajectory.jsonl", max_age=0)

    @app.errorhandler(FileNotFoundError)
    def missing_capture(_error):
        return render_template("error.html", message="That evidence capture or file does not exist."), 404

    @app.errorhandler(EvidenceError)
    def invalid_evidence(error):
        return render_template("error.html", message=str(error)), 409

    @app.errorhandler(413)
    def oversized_request(_error):
        return render_template("error.html", message="Submit a single GitHub Actions run URL."), 413

    return app
