"""Local, inspectable four-question launcher for real three-mode CI trials."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.backend import GitHubTrialBackend
from buildtrace.experiments.models import AgentMode
from buildtrace.experiments.runner import trial_id
from buildtrace.experiments.workflow import load_spec, run_experiment, validate_local_inputs
from buildtrace.github import resolve_github_token


@dataclass(frozen=True)
class Question:
    id: str
    title: str
    prompt: str
    case_id: str
    source_spec: str
    evidence_root: str
    focus: str
    baseline: str


QUESTIONS = (
    Question("p1", "Dependency, API, and cursor", "Repair the CI failure by reconciling the deployed wirekit major, its client constructor, and pagination behavior.",
             "hard-dev-001", "research-hard/experiments/dev-v3/experiment.json", "research-hard/operator/evidence",
             "Check whether version advice from an older run fits this deployment and whether a false-looking cursor is still valid.",
             "Align the current deployed wirekit major and constructor, then preserve a valid cursor such as 0."),
    Question("p2", "Resource missing from sdist", "Repair the release build so a wheel rebuilt from the source distribution contains the runtime JSON resource.",
             "rv2-034", "research-v2/experiments/dev-v2/experiment.json", "research-v2/evidence",
             "Distinguish a missing distribution entry from a working-directory resource lookup problem.",
             "Include the JSON resource in the sdist manifest; verify the wheel rebuilt from that sdist away from the checkout."),
    Question("p3", "HTTP-date Retry-After", "Repair retry timing when Retry-After is an HTTP date, while preserving the current retry contract.",
             "rv2-024", "exploratory-25/experiments/dev-v1/experiment.json", "exploratory-25/operator/evidence",
             "Check date arithmetic against the supplied current instant; reject unrelated historical advice.",
             "Parse the HTTP date relative to the supplied now; return a nonnegative contract-compliant delay."),
    Question("p4", "Binary artifact hash", "Repair the published artifact digest so it hashes the original binary bytes without text normalization.",
             "rv2-041", "exploratory-25/experiments/dev-v1/experiment.json", "exploratory-25/operator/evidence",
             "Check whether a text-decoding insight is applicable to byte-exact artifact integrity.",
             "Hash the original decoded binary payload bytes without lossy UTF-8 decode and re-encode."),
)
QUESTION_BY_ID = {question.id: question for question in QUESTIONS}
MODES = tuple(mode.value for mode in AgentMode)
_JOB_ID = re.compile(r"^q-(?:p1|p2|p3|p4)-[0-9a-f]{12}$")


def _complete_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    data = path.read_bytes()
    if not data.endswith(b"\n"):
        data = data.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in data else b""
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def _complete_json(path: Path):
    if not path.is_file():
        return None
    data = path.read_bytes()
    if not data.endswith(b"\n"):
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


class QuestionLab:
    def __init__(self, data_dir: Path, *, enabled: bool = False, runner=run_experiment):
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / "question-lab"
        self.enabled = enabled
        self.runner = runner
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def _job_path(self, job_id: str) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise KeyError(job_id)
        return self.root / "jobs" / f"{job_id}.json"

    def _output(self, job_id: str) -> Path:
        self._job_path(job_id)
        return self.root / "experiments" / job_id

    def _save(self, job: dict) -> None:
        path = self._job_path(job["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".pending")
        temporary.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
        for attempt in range(20):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def get_job(self, job_id: str) -> dict:
        path = self._job_path(job_id)
        if not path.is_file():
            raise KeyError(job_id)
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("status") == "running" and job_id not in self._threads:
            job["status"] = "interrupted"
        return job

    def jobs(self, question_id: str | None = None) -> list[dict]:
        directory = self.root / "jobs"
        if not directory.is_dir():
            return []
        jobs = []
        for path in directory.glob("q-*.json"):
            try:
                job = self.get_job(path.stem)
            except (KeyError, OSError, ValueError):
                continue
            if question_id is None or job["question_id"] == question_id:
                jobs.append(job)
        return sorted(jobs, key=lambda item: item["id"], reverse=True)

    def source_spec(self, question: Question):
        path = (self.data_dir / question.source_spec).resolve()
        if not path.is_relative_to(self.data_dir) or not path.is_file():
            raise ValueError(f"Registered input for {question.id} is unavailable")
        source = load_spec(path)
        task = next((task for task in source.tasks if task.id == question.case_id), None)
        if task is None or task.split != "dev" or source.schema_version != 2:
            raise ValueError(f"Registered development case for {question.id} is unavailable")
        return source, task

    def evidence_store(self, question: Question) -> EvidenceStore:
        root = (self.data_dir / question.evidence_root).resolve()
        if not root.is_relative_to(self.data_dir):
            raise ValueError("Question evidence root escapes the data directory")
        return EvidenceStore(root)

    def start(self, question_id: str) -> dict:
        if not self.enabled:
            raise ValueError("Start the local viewer with --enable-question-runs to launch paid CI/model trials")
        question = QUESTION_BY_ID.get(question_id)
        if question is None:
            raise KeyError(question_id)
        source, task = self.source_spec(question)
        readiness = validate_local_inputs(source.model_copy(update={"tasks": [task]}), self.evidence_store(question), split="dev")
        if not readiness["local_prerequisites_ready"]:
            raise ValueError("; ".join(readiness["remaining_inputs"]))
        with self._lock:
            if any(job["status"] == "running" for job in self.jobs()):
                raise ValueError("A question run is already active; wait for it to finish")
            job_id = f"q-{question_id}-{uuid.uuid4().hex[:12]}"
            spec = source.model_copy(update={
                "id": job_id, "tasks": [task], "phase_manifest_sha256": None,
                "execution_parallelism": 3, "trials": 3,
                "retrieval": source.retrieval.model_copy(update={"max_insights": 8, "max_characters": 16000}),
            })
            job = {"id": job_id, "question_id": question_id, "case_id": task.id,
                   "status": "running", "error": None}
            self._save(job)
            thread = threading.Thread(target=self._execute, args=(job, spec), name=f"question-{job_id}", daemon=True)
            self._threads[job_id] = thread
            thread.start()
            return job

    def _execute(self, job: dict, spec) -> None:
        try:
            question = QUESTION_BY_ID[job["question_id"]]
            token = resolve_github_token()
            if not token:
                raise ValueError("GitHub authentication became unavailable after launch preflight")

            def backend_factory(task, store, **kwargs):
                return GitHubTrialBackend(task, store, token=token, **kwargs)

            self.runner(spec, self.evidence_store(question), self._output(job["id"]),
                        split="dev", allow_publish=True, continue_on_infra=True,
                        backend_factory=backend_factory)
            status, error = "completed", None
        except Exception as exc:  # preserve a visible failed launch without crashing Flask
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._save({**job, "status": status, "error": error})
            self._threads.pop(job["id"], None)

    def state(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        output = self._output(job_id)
        packets = _complete_json(output / "packets.json")
        packet = packets.get(job["case_id"]) if packets else None
        frozen = output / "experiment.json"
        task = None
        if _complete_json(frozen) is not None:
            spec = load_spec(frozen)
            task = next((item for item in spec.tasks if item.id == job["case_id"]), None)
        trials = []
        for mode in MODES:
            for attempt in range(3):
                identifier = trial_id(spec, task, AgentMode(mode), attempt, "reference") if task else None
                directory = output / "trials" / identifier if identifier else None
                result_path = directory / "result.json" if directory else None
                trace_path = directory / "trajectory.jsonl" if directory else None
                result = _complete_json(result_path) if result_path else None
                rows = _complete_jsonl(trace_path) if trace_path else []
                initial_user = next((row.get("content") for row in rows
                                     if row.get("type") == "message" and row.get("role") == "user"), None)
                packet_delivered = bool(
                    mode != AgentMode.NO_MEMORY.value and packet and isinstance(initial_user, str)
                    and packet["text"] in initial_user
                    and (not result or result.get("packet_sha256") in {None, packet["sha256"]})
                )
                trials.append({
                    "id": identifier, "mode": mode, "attempt": attempt + 1,
                    "status": result["outcome"] if result else "running" if rows else "pending",
                    "stop_reason": result.get("stop_reason") if result else None,
                    "actions": sum(row.get("type") == "function_call" for row in rows),
                    "historical_reads": sum(row.get("type") == "function_call" and row.get("name") == "read_historical_evidence" for row in rows),
                    "raw_reads": sum(row.get("type") == "function_call" and row.get("name") == "read_historical_evidence" and row.get("arguments", {}).get("layer") == "raw" and not row.get("is_error") for row in rows),
                    "packet_insight_ids": result.get("packet_insight_ids", []) if result else [],
                    "packet_delivered": packet_delivered,
                    "packet_count": len(packet["insight_ids"]) if packet_delivered else 0,
                    "verification": (result.get("verifications") or [None])[-1] if result else None,
                })
        return {"job": job, "question": QUESTION_BY_ID[job["question_id"]], "trials": trials,
                "packet": packet, "completed": sum(item["status"] in {"solved", "unsolved", "infra-error", "not-reproduced"} for item in trials)}

    def trace(self, job_id: str, identifier: str) -> dict:
        state = self.state(job_id)
        if identifier not in {item["id"] for item in state["trials"] if item["id"]}:
            raise KeyError(identifier)
        directory = self._output(job_id) / "trials" / identifier
        rows = _complete_jsonl(directory / "trajectory.jsonl")
        if not rows:
            raise KeyError(identifier)
        result_path = directory / "result.json"
        result = _complete_json(result_path)
        return {"state": state, "rows": rows, "result": result,
                "trial": next(item for item in state["trials"] if item["id"] == identifier)}
