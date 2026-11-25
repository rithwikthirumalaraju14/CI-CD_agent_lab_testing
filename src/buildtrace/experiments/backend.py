"""Revision-scoped GitHub trial tools and an independent, trusted CI verification run."""

import base64
import copy
import difflib
import fnmatch
import hashlib
import io
import json
import math
import re
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import yaml

from buildtrace.evidence import EvidenceError, EvidenceStore
from buildtrace.experiments.models import ChangedHunk, TaskSpec, Verification, fingerprint
from buildtrace.github import (
    API_ROOT, GetRetryPolicy, GitHubActionsClient, GitHubError, github_timeout,
    resolve_github_token, retry_github_get,
)
from buildtrace.models import FAILED_CONCLUSIONS, RunReference, SourceFile

_BENCHMARK_SCHEMA = "buildtrace.research-benchmark.v2"
_BENCHMARK_PHASES = ("protected", "resolve", "public", "build", "cli", "wheel_oracle", "sdist_rebuild", "sdist_oracle")
_HOSTED_RUNTIME = ".github/research_v2/verify.py"
_PUBLIC_DESCRIPTOR = ".github/research_v2/public.json"


class BackendError(RuntimeError):
    """An infrastructure failure that cannot be counted as a verified repair."""

    def __init__(self, message: str, *, diagnostics: dict | None = None, elapsed_seconds: float = 0):
        super().__init__(message)
        self.diagnostics = diagnostics or {}
        self.elapsed_seconds = elapsed_seconds
        self.verification: Verification | None = None
        self.hunks: list[ChangedHunk] = []
        self.observation: dict = {}

    def as_dict(self) -> dict:
        """Serializable failure metadata for the coordinator's journal/CI timer."""
        return {
            "message": str(self), "elapsed_seconds": self.elapsed_seconds,
            "diagnostics": copy.deepcopy(self.diagnostics),
            "verification": self.verification.model_dump(mode="json") if self.verification else None,
            "hunks": [hunk.model_dump(mode="json") for hunk in self.hunks],
        }


@dataclass
class PatchResult:
    verification: Verification
    hunks: list[ChangedHunk]
    observation: dict


def safe_repository_path(path: str) -> str:
    if not isinstance(path, str):
        raise ValueError("Use a relative project-file path")
    parsed = PurePosixPath(path)
    if not parsed.parts or parsed.is_absolute() or ".." in parsed.parts or "\\" in path or ":" in path or "\0" in path or parsed.parts[0].lower() == ".git":
        raise ValueError("Use a relative project-file path")
    return parsed.as_posix()


def changed_hunks(originals: dict[str, str | None], overlay: dict[str, str | None]) -> list[ChangedHunk]:
    hunks = []
    for path, content in sorted(overlay.items()):
        before = (originals[path] or "").splitlines(keepends=True)
        after = (content or "").splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
        for group in matcher.get_grouped_opcodes(0):
            old_start, old_end = group[0][1], group[-1][2]
            new_start, new_end = group[0][3], group[-1][4]
            lines = [f"--- a/{path}\n+++ b/{path}\n@@ -{old_start + 1},{old_end - old_start} +{new_start + 1},{new_end - new_start} @@\n"]
            added = deleted = 0
            for tag, a0, a1, b0, b1 in group:
                if tag in {"delete", "replace"}:
                    lines.extend("-" + line for line in before[a0:a1])
                    deleted += a1 - a0
                if tag in {"insert", "replace"}:
                    lines.extend("+" + line for line in after[b0:b1])
                    added += b1 - b0
            diff = "".join(lines)
            hunks.append(ChangedHunk(
                id="hunk-" + hashlib.sha256((path + "\0" + diff).encode()).hexdigest()[:20],
                path=path, added_lines=added, deleted_lines=deleted, diff=diff,
            ))
    return hunks


def required_checks_passed(task: TaskSpec, capture: dict) -> bool:
    if not isinstance(capture.get("run"), dict) or capture["run"].get("conclusion") != "success":
        return False
    if not isinstance(capture.get("jobs"), list) or any(not isinstance(job, dict) for job in capture["jobs"]):
        return False
    for name, required in task.required_steps.items():
        jobs = [job for job in capture["jobs"] if job.get("name") == name]
        if not jobs or any(job.get("conclusion") != "success" for job in jobs):
            return False
        for job in jobs:
            steps = job.get("steps", [])
            if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
                return False
            for step_name in required:
                matches = [step for step in steps if step.get("name") == step_name]
                if not matches or any(step.get("conclusion") != "success" for step in matches):
                    return False
    return True


def _bundle_member(bundle: zipfile.ZipFile, name: str, limit: int = 2 * 1024 * 1024) -> bytes:
    matches = [info for info in bundle.infolist() if PurePosixPath(info.filename).name == name and not info.is_dir()]
    if len(matches) != 1 or matches[0].file_size > limit:
        raise ValueError(f"A unique bounded {name} is required")
    with bundle.open(matches[0]) as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"The {name} evidence is oversized")
    return content


def _distribution_member(bundle: zipfile.ZipFile, relative: str) -> bytes:
    parts = PurePosixPath(relative).parts
    matches = [info for info in bundle.infolist() if not info.is_dir() and PurePosixPath(info.filename).parts[-len(parts):] == parts]
    limit = 20 * 1024 * 1024
    if len(matches) != 1 or matches[0].file_size > limit:
        raise ValueError("A unique bounded original distribution is required")
    with bundle.open(matches[0]) as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Oversized original distribution")
    return content


_BUNDLE_ERRORS = (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, TypeError, RuntimeError, NotImplementedError, OSError, EOFError)


def junit_passed(archive_bytes: bytes, expected_tests: int, report_name: str = "pytest.xml") -> bool:
    """Require all original tests, no failures/errors/skips, from the trusted oracle run."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            root = ET.fromstring(_bundle_member(archive, report_name))
        if type(expected_tests) is not int or expected_tests < 1 or root.tag not in {"testsuites", "testsuite"}:
            return False
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        suite_counts = [int(suite.attrib.get("tests", "0")) for suite in suites]
        tests = sum(suite_counts)
        counts = [int(suite.attrib.get(key, "0")) for suite in suites for key in ("failures", "errors", "skipped")]
        cases = root.findall(".//testcase")
        return (
            bool(suites) and all(value >= 0 for value in suite_counts)
            and tests == expected_tests and all(value == 0 for value in counts)
            and len(cases) == expected_tests
            and all(len(suite.findall("testcase")) == count for suite, count in zip(suites, suite_counts))
            and not any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped"))
        )
    except _BUNDLE_ERRORS:
        return False


def _revision(document: dict, key: str = "sha") -> str:
    sha = document.get(key) if isinstance(document, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BackendError("GitHub returned an invalid revision in a trial operation")
    return sha


def _canonical_json(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _benchmark_contract(content: bytes) -> dict:
    """Validate the pinned deployed contract, independently of its runtime proof."""
    try:
        document = json.loads(content)
        if not isinstance(document, dict) or document.get("schema_version") != _BENCHMARK_SCHEMA:
            raise ValueError("Unsupported benchmark contract")
        if _canonical_json(document) != content:
            raise ValueError("The deployed benchmark contract must use canonical bytes")
        if any(type(document.get(key)) is not int or document[key] < 1 for key in ("expected_public_tests", "expected_oracle_tests")):
            raise ValueError("Separate positive public/oracle counts are required")
        if document.get("required_phases") != list(_BENCHMARK_PHASES) or document.get("require_zero_skips") is not True or document.get("require_exact_test_counts") is not True:
            raise ValueError("The complete exact-count zero-skip contract is required")
        for key in ("protected_sha256", "oracle_sha256"):
            hashes = document.get(key)
            if not isinstance(hashes, dict) or not hashes:
                raise ValueError("Protected project/oracle hash maps are required")
            for path, digest in hashes.items():
                safe_repository_path(path)
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("Invalid protected content hash")
        if not {_HOSTED_RUNTIME, _PUBLIC_DESCRIPTOR, ".github/workflows/" + document["workflow"]} <= document["protected_sha256"].keys():
            raise ValueError("The hosted runtime, descriptor, and workflow must be protected")
        if not {"tests/test_oracle.py", "fixtures/checks.json"} <= document["oracle_sha256"].keys():
            raise ValueError("The separate oracle bundle is incomplete")
        return document
    except (ValueError, TypeError, KeyError) as exc:
        raise BackendError("The trusted hosted benchmark contract is invalid", diagnostics={"reason": str(exc)}) from exc


def _infrastructure_error(exc: Exception) -> BackendError:
    if isinstance(exc, BackendError):
        return exc
    # Preserve useful transport/filesystem diagnostics without serializing
    # request headers, signed URLs, response bodies, or arbitrary exception text.
    return BackendError("CI verification evidence could not be collected or preserved", diagnostics={
        "error_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None),
        "request_attempts": getattr(exc, "retry_attempts", 1),
        "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None),
        "reason": str(exc) if isinstance(exc, (GitHubError, EvidenceError)) else None,
    })


def validate_workflow_environment_change(before: str, after: str, allowed_keys: list[str]):
    """Configuration cases can repair declared env settings while preserving CI commands and checks."""
    def project(text):
        try:
            document = yaml.load(text, Loader=yaml.BaseLoader)
        except yaml.YAMLError as exc:
            raise ValueError("The workflow must remain valid YAML") from exc
        if not isinstance(document, dict):
            raise ValueError("The workflow must remain a YAML mapping")
        document = copy.deepcopy(document)
        environments = {}

        def remove_env(mapping, location):
            environment = mapping.pop("env", {})
            if not isinstance(environment, dict):
                raise ValueError("The controlled CI lab requires explicit environment mappings")
            environments[location] = environment

        remove_env(document, "workflow")
        jobs = document.get("jobs", {})
        if not isinstance(jobs, dict):
            raise ValueError("The workflow must retain its job mappings")
        for name, job in jobs.items():
            if not isinstance(job, dict) or not isinstance(job.get("steps", []), list):
                raise ValueError("The workflow must retain its job and step mappings")
            remove_env(job, "job:" + name)
            for number, step in enumerate(job.get("steps", [])):
                if not isinstance(step, dict):
                    raise ValueError("The workflow must retain its step mappings")
                remove_env(step, f"job:{name}/step:{number}")
        return document, environments

    original, old_env = project(before)
    proposed, new_env = project(after)
    if original != proposed:
        raise ValueError("Workflow repairs may change declared environment settings, not commands, triggers, or checks")
    for location in old_env.keys() | new_env.keys():
        old, new = old_env.get(location, {}), new_env.get(location, {})
        for key in old.keys() | new.keys():
            if old.get(key) != new.get(key) and key not in allowed_keys:
                raise ValueError(f"The task does not allow changing workflow environment key {key}")


class GitHubTrialBackend:
    def __init__(
        self, task: TaskSpec, store: EvidenceStore, *, token: str | None = None,
        client: httpx.Client | None = None, allow_publish: bool = False, namespace: str,
        artifact_prefix: str | None = None, retry_policy: GetRetryPolicy | None = None,
        cancel_overdue_runs: bool = True,
    ):
        self.task, self.store = task, store
        self.token = resolve_github_token() if token is None else token
        if not self.token:
            raise BackendError("Configure GitHub authentication for trial branches and Actions runs.")
        if not allow_publish:
            raise BackendError("Live experiments create trial branches and run CI. Pass --allow-publish to authorize them.")
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,50}", namespace):
            raise ValueError("Invalid trial namespace")
        self.artifact_prefix = artifact_prefix if artifact_prefix is not None else getattr(task, "artifact_prefix", "invoice-ci")
        if not isinstance(self.artifact_prefix, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", self.artifact_prefix):
            raise ValueError("Invalid verification artifact prefix")
        self.namespace = namespace
        self.retry_policy = retry_policy or GetRetryPolicy()
        self.cancel_overdue_runs = cancel_overdue_runs
        self._supports_dispatch_id = False
        self._dispatches = {}
        self._trial_branches = set()
        self._operation_deadline = None
        self.ci_operation_seconds = 0.0
        self.last_failure: BackendError | None = None
        self.client = client or httpx.Client(timeout=20)
        self._owns_client = client is None
        self.headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        self.base = f"/repos/{task.repository}"
        self.overlay, self.originals = {}, {}
        self.attempt = 0
        self.allowed_captures = {task.capture_id}
        capture = store.load(task.capture_id)
        if task.capture_sha256 is not None and fingerprint(capture) != task.capture_sha256:
            raise ValueError("The task's initial evidence manifest was modified")
        store.verify(task.capture_id)
        reference = RunReference.parse(task.initial_run_url)
        if (
            capture["source_ref"] != task.base_sha
            or capture["reference"]["run_id"] != reference.run_id
            or capture["reference"]["attempt"] != reference.attempt
            or capture["reference"]["owner"] + "/" + capture["reference"]["repository"] != task.repository
        ):
            raise ValueError("The archived CI attempt does not match the task snapshot")
        if capture["run"].get("event") not in {"push", "workflow_dispatch"}:
            raise ValueError("The initial CI benchmark supports push and manual-dispatch snapshots")
        if capture["run"].get("conclusion") not in FAILED_CONCLUSIONS:
            raise ValueError("The input must be a known failing CI run")
        self.initial_capture = capture

    def close(self):
        if self._owns_client:
            self.client.close()

    def _api(self, method, path, payload=None):
        method = method.upper()
        deadline = getattr(self, "_operation_deadline", None)

        def request():
            if deadline is not None and time.monotonic() >= deadline:
                raise BackendError("The trial's CI verification deadline was reached")
            response = self.client.request(
                method, API_ROOT + self.base + path, headers=self.headers, json=payload,
                timeout=github_timeout(deadline), follow_redirects=False,
            )
            try:
                GitHubActionsClient._check_status(response)
                body = response.json() if response.content else {}
                if not isinstance(body, dict):
                    raise ValueError("Expected a GitHub object response")
                return body
            finally:
                response.close()

        try:
            if method == "GET":
                return retry_github_get(request, policy=getattr(self, "retry_policy", None), deadline=deadline)
            # Dispatches, commits, refs, and cancellation requests are attempted once.
            return request()
        except (httpx.RequestError, GitHubError, ValueError) as exc:
            status = getattr(exc, "status_code", None)
            raise BackendError(f"GitHub trial operation failed ({method} {path.split('?')[0]}).", diagnostics={
                "method": method, "path": path.split("?")[0], "status_code": status,
                "error_type": type(exc).__name__, "request_attempts": getattr(exc, "retry_attempts", 1),
                "ambiguous_mutation": method != "GET" and (
                    isinstance(exc, (httpx.RequestError, ValueError)) or status is not None and 500 <= status < 600
                ),
            }) from exc

    def pin_oracle(self) -> TaskSpec:
        ref = self._api("GET", "/git/ref/heads/" + quote(self.task.oracle_ref, safe=""))
        sha = _revision(ref.get("object"))
        if self.task.oracle_sha is not None and sha != self.task.oracle_sha:
            raise BackendError("The trusted verification branch moved; create a new, explicitly pinned experiment.")
        workflow = self._api("GET", "/contents/.github/workflows/" + quote(self.task.workflow, safe="") + f"?ref={sha}")
        try:
            if workflow.get("type", "file") != "file" or workflow.get("encoding", "base64") != "base64" or not isinstance(workflow.get("content"), str):
                raise ValueError("Expected a workflow file")
            document = yaml.safe_load(base64.b64decode("".join(workflow["content"].split()), validate=True))
        except (ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            raise BackendError("The trusted verification workflow could not be decoded") from exc
        if not isinstance(document, dict):
            raise BackendError("The trusted verification workflow must be a YAML mapping")
        # PyYAML's YAML 1.1 parser may read the GitHub key 'on' as True.
        triggers = document.get("on", document.get(True, {}))
        dispatch = triggers.get("workflow_dispatch") if isinstance(triggers, dict) else None
        inputs = dispatch.get("inputs", {}) if isinstance(dispatch, dict) else {}
        if not isinstance(inputs, dict) or not {"source_sha", "oracle_mode"}.issubset(inputs):
            raise BackendError("Publish the lab workflow's trusted source_sha/oracle_mode inputs before live experiments.")
        self._supports_dispatch_id = "dispatch_id" in inputs
        updates = {"oracle_sha": sha, "capture_sha256": fingerprint(self.initial_capture)}
        contract_path = getattr(self.task, "oracle_contract_path", None)
        configured_hash = getattr(self.task, "verification_contract_sha256", None)
        if self._verification_schema() == 2:
            if not self._supports_dispatch_id:
                raise BackendError("The hosted v2 workflow must support unique dispatch_id run names")
            contract_path = contract_path or "infra/research_v2/contract.json"
            content = self._trusted_file(contract_path, sha)
            contract = _benchmark_contract(content)
            digest = hashlib.sha256(content).hexdigest()
            if configured_hash is not None and configured_hash != digest:
                raise BackendError("The pinned trusted verification contract hash does not match the task")
            oracle_count = getattr(self.task, "expected_oracle_test_count", None)
            if contract["expected_public_tests"] != self.task.expected_test_count or oracle_count is not None and oracle_count != contract["expected_oracle_tests"]:
                raise BackendError("The task's public/oracle test counts do not match the trusted contract")
            if contract["workflow"] != self.task.workflow:
                raise BackendError("The trusted contract names a different workflow")
            protected = contract["protected_sha256"]
            raw_workflow = base64.b64decode("".join(workflow["content"].split()), validate=True)
            if hashlib.sha256(raw_workflow).hexdigest() != protected[".github/workflows/" + self.task.workflow]:
                raise BackendError("The trusted workflow differs from the protected deployment contract")
            for path in (_HOSTED_RUNTIME, _PUBLIC_DESCRIPTOR):
                if hashlib.sha256(self._trusted_file(path, sha)).hexdigest() != protected[path]:
                    raise BackendError("A trusted hosted helper/descriptor differs from the pinned contract")
            oracle_root = str(PurePosixPath(contract_path).parent / "oracle")
            for path, expected in contract["oracle_sha256"].items():
                if hashlib.sha256(self._trusted_file(oracle_root + "/" + path, sha)).hexdigest() != expected:
                    raise BackendError("The separate trusted oracle bundle differs from its pinned contract")
            self._verification_contract_sha256 = digest
            self._oracle_test_count = contract["expected_oracle_tests"]
            updates.update(verification_contract_sha256=digest, expected_oracle_test_count=self._oracle_test_count, oracle_contract_path=contract_path)
        elif configured_hash is not None or contract_path is not None and "oracle_contract_path" in getattr(self.task, "model_fields_set", {"oracle_contract_path"}):
            # Explicit legacy helper paths are still checked without changing
            # invoice/v1 defaults or imposing the v2 oracle format on old tasks.
            content = self._trusted_file(contract_path or "infra/research_v2/contract.json", sha)
            try:
                digest = hashlib.sha256(_canonical_json(json.loads(content))).hexdigest()
            except (ValueError, TypeError) as exc:
                raise BackendError("The configured trusted contract is not valid JSON") from exc
            if configured_hash is not None and digest != configured_hash:
                raise BackendError("The configured trusted contract hash does not match the task")
        self.task = self.task.model_copy(update=updates)
        return self.task

    def _verification_schema(self):
        return getattr(self.task, "schema_version", 1)

    def _expected_oracle_tests(self):
        count = getattr(self.task, "expected_oracle_test_count", None)
        return count if count is not None else getattr(self, "_oracle_test_count", self.task.expected_test_count)

    def _trusted_file(self, path, sha):
        try:
            path = safe_repository_path(path)
        except ValueError as exc:
            raise BackendError("Use a safe repository-relative trusted contract path") from exc
        body = self._api("GET", "/contents/" + quote(path, safe="/") + f"?ref={sha}")
        try:
            if body.get("type") != "file" or body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
                raise ValueError("Expected a trusted file")
            content = base64.b64decode("".join(body["content"].split()), validate=True)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError("Oversized trusted file")
            return content
        except (ValueError, TypeError, KeyError) as exc:
            raise BackendError("Could not validate a configured trusted verification path", diagnostics={"path": path}) from exc

    def initial_observation(self) -> dict:
        return {
            "repository": self.task.repository, "base_sha": self.task.base_sha,
            "initial_run_url": self.task.initial_run_url,
            "failed_jobs": [{"name": job.get("name"), "steps": [step.get("name") for step in job.get("steps", []) if step.get("conclusion") in FAILED_CONCLUSIONS]} for job in self.initial_capture["jobs"] if job.get("conclusion") in FAILED_CONCLUSIONS],
            "current_logs": self._log_catalog(self.initial_capture),
        }

    @staticmethod
    def _log_catalog(capture):
        return [{"entry_id": f"{capture['id']}/{entry['id']}", "name": entry["label"], "bytes": entry["byte_count"]} for entry in capture["entries"] if entry["kind"] == "log"]

    def list_files(self):
        tree = self._api("GET", f"/git/trees/{self.task.base_sha}?recursive=1")
        if tree.get("truncated"):
            raise BackendError("The task's file tree was truncated by GitHub")
        entries = tree.get("tree")
        if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("path"), str) for item in entries):
            raise BackendError("GitHub returned an invalid task file tree")
        base_paths = {item["path"] for item in entries if item.get("type") == "blob"}
        base_paths.update(path for path, content in self.overlay.items() if content is not None)
        base_paths.difference_update(path for path, content in self.overlay.items() if content is None)
        return sorted(base_paths)

    def _read_base(self, path):
        if path not in self.originals:
            deadline = getattr(self, "_operation_deadline", None)
            missing = object()

            def get():
                response = self.client.get(
                    API_ROOT + self.base + "/contents/" + quote(path, safe="/") + f"?ref={self.task.base_sha}",
                    headers=self.headers, timeout=github_timeout(deadline), follow_redirects=False,
                )
                try:
                    if response.status_code == 404:
                        return missing
                    GitHubActionsClient._check_status(response)
                    return response.json()
                finally:
                    response.close()

            try:
                body = retry_github_get(get, policy=getattr(self, "retry_policy", None), deadline=deadline)
                if body is missing:
                    self.originals[path] = None
                    return None
            except (httpx.RequestError, GitHubError, ValueError) as exc:
                raise BackendError("Could not read a file from the fixed task snapshot", diagnostics={
                    "method": "GET", "path": path, "status_code": getattr(exc, "status_code", None),
                    "error_type": type(exc).__name__, "request_attempts": getattr(exc, "retry_attempts", 1),
                }) from exc
            if isinstance(body, list):
                raise ValueError("This path is a directory. Use list_current_files and read an exact file path.")
            if not isinstance(body, dict):
                raise BackendError("GitHub returned an invalid source-file response")
            if body.get("type") == "dir":
                raise ValueError("This path is a directory. Use list_current_files and read an exact file path.")
            if body.get("type") not in {"file", "symlink", "submodule"}:
                raise BackendError("GitHub returned invalid source-file metadata")
            if body.get("type") != "file":
                raise ValueError("Choose a regular project file, not a directory or symbolic link")
            if body.get("encoding") != "base64" or type(body.get("size", 0)) is not int or not isinstance(body.get("content"), str):
                raise BackendError("GitHub returned an unsupported source-file representation")
            if body.get("size", 0) > 100000:
                raise ValueError("This source file exceeds the current agent's 100,000-byte limit")
            try:
                content = base64.b64decode("".join(body["content"].split()), validate=True)
            except (ValueError, KeyError, TypeError) as exc:
                raise BackendError("Could not read a file from the fixed task snapshot") from exc
            if len(content) > 100000:
                raise ValueError("This source file exceeds the current agent's 100,000-byte limit")
            try:
                self.originals[path] = content.decode("utf-8")
            except UnicodeError as exc:
                raise ValueError("Choose a UTF-8 project source file") from exc
        return self.originals[path]

    def read_file(self, path):
        path = safe_repository_path(path)
        content = self.overlay[path] if path in self.overlay else self._read_base(path)
        if content is None:
            raise ValueError("This file does not exist in the current task state")
        if len(content) > 30000:
            raise ValueError("This source file exceeds the current agent's 30,000-character limit")
        return content

    def read_log(self, entry_id, offset=0, limit=12000):
        if not isinstance(entry_id, str):
            raise ValueError("Choose a string entry ID from the current-log catalog")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
            raise ValueError("Invalid current-log range")
        capture_id, separator, file_id = entry_id.partition("/")
        if not separator or capture_id not in self.allowed_captures:
            raise ValueError("Only this trial's current CI evidence is accessible")
        manifest = self.store.load(capture_id)
        if not any(entry["id"] == file_id for entry in manifest["entries"]):
            raise ValueError("Choose an entry from the current-log catalog")
        entry, content = self.store.read_entry(capture_id, file_id)
        if entry["kind"] != "log":
            raise ValueError("Choose an entry from the current-log catalog")
        text = content.decode("utf-8", errors="replace")
        if offset > len(text):
            raise ValueError("Current-log offset is outside the document")
        excerpt = text[offset:offset + limit]
        return {"text": excerpt, "offset": offset, "next_offset": offset + len(excerpt) if offset + len(excerpt) < len(text) else None, "sha256": entry["sha256"]}

    def _check_changes(self, changes):
        if not isinstance(changes, list) or not 1 <= len(changes) <= 6:
            raise ValueError("Submit between one and six changed files")
        updated, seen = self.overlay.copy(), set()
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"path", "content"}:
                raise ValueError("Each change needs only path and content")
            path = safe_repository_path(change["path"])
            if path in seen:
                raise ValueError("A patch cannot repeat a file path")
            seen.add(path)
            protected = any(path.startswith(pattern) if pattern.endswith("/") else fnmatch.fnmatchcase(path, pattern) for pattern in self.task.protected_paths)
            if protected or not any(fnmatch.fnmatchcase(path, pattern) for pattern in self.task.editable_paths):
                raise ValueError(f"The task does not allow changing {path}")
            content = change["content"]
            if content is not None and (not isinstance(content, str) or len(content.encode("utf-8")) > 100000):
                raise ValueError("Each new file must contain at most 100,000 UTF-8 bytes")
            self._read_base(path)
            if path.startswith(".github/workflows/"):
                if path != f".github/workflows/{self.task.workflow}" or self.originals[path] is None or content is None:
                    raise ValueError("Only the existing target workflow's declared environment settings are editable")
                validate_workflow_environment_change(self.originals[path], content, self.task.editable_environment_keys)
            updated[path] = content
        if not any(content != self.originals[path] for path, content in updated.items()):
            raise ValueError("The proposed patch does not change the failing snapshot")
        return updated

    def _dispatch_and_capture(self, ref, expected_head, inputs, deadline, expected_title=None, archive_oracle=False):
        start = time.monotonic()
        previous_deadline = getattr(self, "_operation_deadline", None)
        self._operation_deadline = min(previous_deadline, deadline) if previous_deadline is not None else deadline
        try:
            return self._capture_dispatch(ref, expected_head, inputs, deadline, expected_title, archive_oracle)
        except (BackendError, EvidenceError, GitHubError, httpx.RequestError, OSError) as exc:
            error = _infrastructure_error(exc)
            state = getattr(self, "_dispatches", {}).get((ref, expected_head, json.dumps(inputs, sort_keys=True)))
            if state is not None:
                if time.monotonic() >= deadline and not state["capture_id"]:
                    self._cancel_overdue_dispatch(state)
                error.diagnostics.update(self._dispatch_diagnostics(state))
            error.elapsed_seconds = time.monotonic() - start
            if error is exc:
                raise
            raise error from exc
        finally:
            self._operation_deadline = previous_deadline

    def _capture_dispatch(self, ref, expected_head, inputs, deadline, expected_title, archive_oracle):
        if time.monotonic() >= deadline:
            raise BackendError("The trial's CI verification deadline was reached")
        path = "/actions/workflows/" + quote(self.task.workflow, safe="") + "/runs"
        query = "?event=workflow_dispatch&branch=" + quote(ref, safe="") + f"&head_sha={expected_head}&per_page=100"
        if not hasattr(self, "_dispatches"):
            self._dispatches = {}
        key = (ref, expected_head, json.dumps(inputs, sort_keys=True))
        state = self._dispatches.get(key)
        if state is None:
            existing = {run["id"] for run in self._workflow_runs(self._api("GET", path + query))}
            dispatch_inputs = inputs.copy()
            dispatch_id = None
            if getattr(self, "_supports_dispatch_id", False):
                dispatch_id = f"{getattr(self, 'namespace', 'trial')}-p{getattr(self, 'attempt', 0)}-{uuid.uuid4().hex[:16]}"
                dispatch_inputs["dispatch_id"] = dispatch_id
                mode = "oracle" if archive_oracle else "target"
                source_sha = inputs.get("source_sha") or expected_head
                expected_title = f"BuildTrace {mode} {source_sha} {dispatch_id}"
            state = {
                "ref": ref, "expected_head": expected_head, "expected_title": expected_title,
                "dispatch_id": dispatch_id, "attempt": getattr(self, "attempt", 0),
                "kind": "oracle" if archive_oracle else "target", "existing_ids": sorted(existing),
                "runs": [], "capture_id": None, "dispatch_error": None, "rejected": False,
            }
            # Record the intent before sending. A lost response must never cause a second POST.
            self._dispatches[key] = state
            try:
                self._api("POST", "/actions/workflows/" + quote(self.task.workflow, safe="") + "/dispatches", {"ref": ref, "inputs": dispatch_inputs})
            except BackendError as exc:
                state["dispatch_error"] = exc.as_dict()
                if not exc.diagnostics.get("ambiguous_mutation"):
                    state["rejected"] = True
                    raise
                # The request may have been accepted. Reconcile by reads only.
        if state["rejected"]:
            raise BackendError("The previous dispatch was rejected; it was not repeated", diagnostics=self._dispatch_diagnostics(state))
        if state.get("capture_error"):
            raise BackendError("The dispatched run's evidence could not be archived", diagnostics=self._dispatch_diagnostics(state))
        if state["capture_id"]:
            return self.store.load(state["capture_id"])
        while time.monotonic() < deadline:
            runs = self._workflow_runs(self._api("GET", path + query))
            candidates = [run for run in runs if (
                run["id"] not in state["existing_ids"] and run.get("head_sha") == expected_head
                and run.get("event", "workflow_dispatch") == "workflow_dispatch"
                and run.get("head_branch", ref) == ref
                and (state["expected_title"] is None or run.get("display_title") == state["expected_title"])
            )]
            if candidates:
                state["runs"] = [{key: run.get(key) for key in (
                    "id", "html_url", "run_attempt", "head_sha", "head_branch", "event", "display_title", "status", "conclusion",
                )} for run in candidates]
            if len(candidates) > 1:
                raise BackendError("Multiple new CI runs matched one dispatch; attribution is ambiguous", diagnostics=self._dispatch_diagnostics(state))
            if candidates and candidates[0].get("status") == "completed":
                run = candidates[0]
                try:
                    reference = RunReference.parse(run["html_url"] + f"/attempts/{run.get('run_attempt', 1)}")
                    if reference.run_id != run["id"] or reference.full_name != self.task.repository:
                        raise ValueError("Mismatched dispatched run")
                except (ValueError, KeyError, TypeError) as exc:
                    raise BackendError("GitHub returned an invalid dispatched run reference", diagnostics=self._dispatch_diagnostics(state)) from exc
                attachment_error = None
                with GitHubActionsClient(token=self.token, client=self.client, retry_policy=getattr(self, "retry_policy", None), deadline=deadline) as client:
                    # GitHub's run listing can say completed while the attempt
                    # and jobs endpoints still return an earlier in-progress
                    # snapshot. Archive only terminal evidence; otherwise a
                    # successful patch can be misclassified as a failed check.
                    while True:
                        collected = client.collect(reference)
                        run_complete = collected.run.get("status") == "completed" and collected.run.get("conclusion") is not None
                        jobs_complete = all(
                            job.get("status") == "completed"
                            or (job.get("status") is None and job.get("conclusion") is not None)
                            for job in collected.jobs
                        )
                        if run_complete and jobs_complete:
                            break
                        if time.monotonic() >= deadline:
                            raise BackendError("GitHub's completed-run listing still has nonterminal attempt or job evidence",
                                               diagnostics=self._dispatch_diagnostics(state))
                        time.sleep(min(5, max(0, deadline - time.monotonic())))
                    if archive_oracle or self._verification_schema() == 2:
                        try:
                            if archive_oracle:
                                self._attach_oracle_evidence(collected, client)
                            else:
                                self._attach_oracle_evidence(collected, client, archive_label="public-evidence.zip")
                        except BackendError as exc:
                            attachment_error = exc
                            collected.warnings.append(str(exc))
                    capture = self.store.save(collected)
                state["capture_id"] = capture["id"]
                self.allowed_captures.add(capture["id"])
                if attachment_error is not None:
                    state["capture_error"] = attachment_error.as_dict()
                    attachment_error.diagnostics.update(self._dispatch_diagnostics(state))
                    raise attachment_error
                return capture
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        state["cancellation"] = self._cancel_overdue_dispatch(state)
        raise BackendError("The trial's CI verification deadline was reached", diagnostics=self._dispatch_diagnostics(state))

    @staticmethod
    def _workflow_runs(body):
        runs = body.get("workflow_runs") if isinstance(body, dict) else None
        if not isinstance(runs, list) or any(not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] < 1 for run in runs):
            raise BackendError("GitHub returned an invalid dispatched-run listing")
        return runs

    @staticmethod
    def _dispatch_diagnostics(state):
        return {key: state[key] for key in (
            "kind", "ref", "expected_head", "expected_title", "dispatch_id", "runs", "capture_id", "dispatch_error", "cancellation",
        ) if key in state}

    def _cancel_overdue_dispatch(self, state):
        """At most one cancel POST, for an attributed run from this dispatch only."""
        if "cancellation" not in state:
            try:
                state["cancellation"] = self._cancel_known_run(state)
            except (GitHubError, httpx.RequestError, OSError) as exc:
                state["cancellation"] = {"status": "request-failed", "error": _infrastructure_error(exc).as_dict()}
        return state["cancellation"]

    def _cancel_known_run(self, state):
        if not getattr(self, "cancel_overdue_runs", True) or len(state["runs"]) != 1:
            return {"status": "not-requested"}
        owned = state["dispatch_id"] is not None or state["ref"] in getattr(self, "_trial_branches", set())
        if not owned:
            return {"status": "not-attributable"}
        observed = state["runs"][0]
        run_id = observed["id"]
        previous_deadline = getattr(self, "_operation_deadline", None)
        # Cleanup has its own small budget; an expired verification budget must
        # not turn into unbounded polling or cancellation of unrelated work.
        self._operation_deadline = time.monotonic() + 5
        try:
            current = self._api("GET", f"/actions/runs/{run_id}")
            if current.get("status") == "completed":
                return {"run_id": run_id, "status": "already-completed"}
            if (
                current.get("id") != run_id or current.get("head_sha") != state["expected_head"]
                or current.get("head_branch") != state["ref"] or current.get("event") != "workflow_dispatch"
                or current.get("run_attempt", 1) != (observed.get("run_attempt") or 1)
                or current.get("status") not in {"queued", "in_progress", "waiting", "pending", "requested"}
                or state["expected_title"] is not None and current.get("display_title") != state["expected_title"]
            ):
                return {"run_id": run_id, "status": "identity-not-confirmed"}
            self._api("POST", f"/actions/runs/{run_id}/cancel")
            return {"run_id": run_id, "status": "requested"}
        except BackendError as exc:
            return {"run_id": run_id, "status": "request-failed", "error": exc.as_dict()}
        finally:
            self._operation_deadline = previous_deadline

    def _artifact_prefix(self):
        return getattr(self, "artifact_prefix", getattr(self.task, "artifact_prefix", "invoice-ci"))

    def _attach_oracle_evidence(self, collected, client, *, archive_label="oracle-evidence.zip"):
        name = f"{self._artifact_prefix()}-{collected.reference.run_id}-{collected.reference.attempt}"
        role = "oracle" if archive_label == "oracle-evidence.zip" else "public verifier"
        listing_error = getattr(client, "collection_errors", {}).get("artifacts")
        if listing_error is not None:
            raise BackendError(f"Could not list the {role}'s original verification artifacts", diagnostics={
                "artifact_name": name, "artifact_listing_error": listing_error,
            })
        artifacts = [artifact for artifact in collected.artifacts if artifact.get("name") == name and not artifact.get("expired")]
        if len(artifacts) != 1:
            collected.warnings.append(f"A unique current-attempt {role} artifact was not available.")
            return
        try:
            archive = client._download_logs(f"{self.base}/actions/artifacts/{artifacts[0]['id']}/zip")
        except GitHubError as exc:
            raise BackendError(f"Could not download the {role}'s original verification artifact", diagnostics={
                "artifact_name": name, "status_code": exc.status_code,
                "request_attempts": getattr(exc, "retry_attempts", 1),
            }) from exc
        collected.attachments.append(SourceFile(archive_label, archive))
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                for name in ("pytest.xml", "environment-before-install.json", "verification-proof.json", "verification-contract.json", "wheel-oracle.xml", "sdist-oracle.xml", "phase-results.json"):
                    if any(PurePosixPath(info.filename).name == name for info in bundle.infolist()):
                        collected.attachments.append(SourceFile(name, _bundle_member(bundle, name)))
        except _BUNDLE_ERRORS:
            collected.warnings.append("The oracle artifact could not be decoded.")

    def _oracle_passed(self, capture, candidate_sha):
        return not self._oracle_failures(capture, candidate_sha)

    def _oracle_failures(self, capture, candidate_sha):
        if self._verification_schema() == 2:
            return self._hosted_failures(capture, candidate_sha, oracle=True)
        failures = []
        if not required_checks_passed(self.task, capture):
            failures.append("The trusted oracle's required jobs/steps did not all pass")
        entries = [entry for entry in capture["entries"] if entry["kind"] == "artifact" and entry["label"] == "oracle-evidence.zip"]
        if len(entries) != 1:
            return failures + ["A unique original oracle artifact is missing"]
        _, archive = self.store.read_entry(capture["id"], entries[0]["id"])
        expected_oracle_tests = self._expected_oracle_tests()
        if not junit_passed(archive, expected_oracle_tests):
            failures.append("The protected original test count or JUnit results did not pass")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                environment = json.loads(_bundle_member(bundle, "environment-before-install.json", 100000))
                if not isinstance(environment, dict) or environment.get("checked_out_sha") != candidate_sha:
                    failures.append("The oracle's checked_out_sha does not prove the candidate revision")
                # The invoice pilot predates this proof. All new artifact contracts
                # require trusted counts, test integrity, workflow SHA, and run identity.
                if self._artifact_prefix() != "invoice-ci":
                    proof = json.loads(_bundle_member(bundle, "verification-proof.json", 100000))
                    reference = capture["reference"]
                    if not isinstance(proof, dict) or not (
                        type(proof.get("schema_version")) is int and proof["schema_version"] == 1
                        and proof.get("oracle_mode") is True
                        and proof.get("checked_out_sha") == candidate_sha
                        and proof.get("workflow_sha") == self.task.oracle_sha
                        and capture["run"].get("head_sha") == self.task.oracle_sha
                        and type(proof.get("expected_test_count")) is int
                        and proof["expected_test_count"] == expected_oracle_tests
                        and type(proof.get("observed_test_count")) is int
                        and proof["observed_test_count"] == expected_oracle_tests
                        and proof.get("protected_tests_unchanged") is True
                        and proof.get("verification_complete") is True
                        and isinstance(proof.get("protected_tests_sha256"), str)
                        and re.fullmatch(r"[0-9a-f]{64}", proof["protected_tests_sha256"])
                        and type(proof.get("run_id")) is int and proof["run_id"] == reference["run_id"]
                        and type(proof.get("run_attempt")) is int and proof["run_attempt"] == reference["attempt"]
                    ):
                        failures.append("The generic trusted verification proof is invalid")
        except _BUNDLE_ERRORS:
            failures.append("The oracle's revision/count proof is missing or unreadable")
        return failures

    def _hosted_failures(self, capture, candidate_sha, *, oracle):
        """V2 requires independent full-phase, three-report, contract-bound proof."""
        failures = []
        role = "oracle" if oracle else "public verifier"
        if not required_checks_passed(self.task, capture):
            failures.append(f"The {role}'s required jobs/steps did not all pass")
        label = "oracle-evidence.zip" if oracle else "public-evidence.zip"
        entries = [entry for entry in capture["entries"] if entry["kind"] == "artifact" and entry["label"] == label]
        if len(entries) != 1:
            return failures + [f"A unique original {role} artifact is missing"]
        _, archive = self.store.read_entry(capture["id"], entries[0]["id"])
        expected_public, expected_oracle = self.task.expected_test_count, self._expected_oracle_tests()
        reports = {"public": ("pytest.xml", expected_public)}
        if oracle:
            reports.update({"wheel_oracle": ("wheel-oracle.xml", expected_oracle), "sdist_oracle": ("sdist-oracle.xml", expected_oracle)})
        for phase, (filename, count) in reports.items():
            if not junit_passed(archive, count, filename):
                failures.append(f"The {phase} exact-count, zero-skip JUnit proof did not pass")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                environment = json.loads(_bundle_member(bundle, "environment-before-install.json", 100000))
                proof = json.loads(_bundle_member(bundle, "verification-proof.json"))
                if not isinstance(environment, dict) or not isinstance(proof, dict):
                    raise ValueError("Expected hosted proof objects")
                workflow_sha = self.task.oracle_sha if oracle else candidate_sha
                reference = capture["reference"]
                for document in (environment, proof):
                    if not (
                        type(document.get("schema_version")) is int and document["schema_version"] == 2
                        and document.get("contract_schema") == _BENCHMARK_SCHEMA
                        and document.get("oracle_mode") is oracle
                        and document.get("checked_out_sha") == candidate_sha
                        and isinstance(workflow_sha, str) and document.get("workflow_sha") == workflow_sha
                        and capture["run"].get("head_sha") == workflow_sha
                        and type(document.get("run_id")) is int and document["run_id"] == reference["run_id"]
                        and type(document.get("run_attempt")) is int and document["run_attempt"] == reference["attempt"]
                        and type(document.get("expected_public_tests")) is int and document["expected_public_tests"] == expected_public
                    ):
                        failures.append(f"The {role}'s candidate/workflow/run identity proof is invalid")
                        break
                if proof.get("protected_files_unchanged") is not True or proof.get("verification_complete") is not True:
                    failures.append("The complete protected hosted verification did not pass")
                required = _BENCHMARK_PHASES if oracle else _BENCHMARK_PHASES[:5]
                phases = proof.get("phases")
                if not isinstance(phases, list) or any(not isinstance(phase, dict) for phase in phases):
                    raise ValueError("Missing hosted phases")
                names = [phase.get("phase") for phase in phases]
                if len(phases) != len(required) or set(names) != set(required) or len(set(names)) != len(names):
                    failures.append("The full hosted phase set is missing, duplicated, or incomplete")
                by_name = {phase.get("phase"): phase for phase in phases if isinstance(phase.get("phase"), str)}
                if any(phase.get("passed") is not True or type(phase.get("returncode")) is not int or phase["returncode"] != 0 for phase in phases):
                    failures.append("At least one hosted verification phase did not pass")
                for name, (filename, count) in reports.items():
                    phase = by_name.get(name, {})
                    counts = phase.get("counts", {})
                    if not isinstance(counts, dict) or not (
                        type(phase.get("tests")) is int and phase["tests"] == count and phase.get("junit") == filename
                        and counts.get("passed") is True
                        and all(type(counts.get(key)) is int and counts[key] == expected for key, expected in (("tests", count), ("failures", 0), ("errors", 0), ("skipped", 0)))
                    ):
                        failures.append(f"The {name} observed count proof does not match the task")
                smoke = proof.get("public_distribution")
                if not isinstance(smoke, dict) or smoke.get("phase") != "distribution_smoke" or smoke.get("passed") is not True or type(smoke.get("returncode")) is not int or smoke["returncode"] != 0:
                    failures.append("The public direct/rebuilt distribution smoke checks did not pass")
                distributions = proof.get("distributions")
                if not isinstance(distributions, dict):
                    raise ValueError("Missing distribution identities")
                for name in (("direct", "sdist", "rebuilt") if oracle else ("direct", "sdist")):
                    record = distributions.get(name, {})
                    filename = record.get("filename") if isinstance(record, dict) else None
                    if not isinstance(filename, str) or not filename or PurePosixPath(filename).name != filename or "\\" in filename or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256"))):
                        raise ValueError("Invalid distribution identity proof")
                    expected_suffix = ".tar.gz" if name == "sdist" else ".whl"
                    location = "rebuilt" if name == "rebuilt" else "direct"
                    content = _distribution_member(bundle, f"distributions/{location}/{filename}")
                    if not filename.endswith(expected_suffix) or hashlib.sha256(content).hexdigest() != record["sha256"]:
                        failures.append(f"The original {name} distribution bytes do not match their proof")
                if oracle:
                    content = _bundle_member(bundle, "verification-contract.json")
                    try:
                        contract = _benchmark_contract(content)
                    except BackendError as exc:
                        raise ValueError("Invalid captured hosted contract") from exc
                    expected_hash = getattr(self.task, "verification_contract_sha256", None) or getattr(self, "_verification_contract_sha256", None)
                    digest = hashlib.sha256(content).hexdigest()
                    if not isinstance(expected_hash, str) or digest != expected_hash or any(document.get("verification_contract_sha256") != expected_hash for document in (environment, proof)):
                        failures.append("The trusted verification contract hash does not match the pinned task")
                    if contract["expected_public_tests"] != expected_public or contract["expected_oracle_tests"] != expected_oracle or any(type(document.get("expected_oracle_tests")) is not int or document["expected_oracle_tests"] != expected_oracle for document in (environment, proof)):
                        failures.append("The separate oracle count does not match the trusted contract/task")
                    if proof.get("protected_tree_sha256") != hashlib.sha256(_canonical_json(contract["protected_sha256"])).hexdigest():
                        failures.append("The generic protected tree digest does not match the contract")
                    origins = []
                    for name, distribution in (("wheel_oracle", "direct"), ("sdist_oracle", "rebuilt")):
                        origin = by_name.get(name, {}).get("origin", {})
                        if not isinstance(origin, dict):
                            raise ValueError("Missing distribution import-origin proof")
                        wheel = str(origin.get("wheel", "")).replace("\\", "/")
                        module = str(origin.get("module_file", "")).replace("\\", "/")
                        record = distributions[distribution]
                        if not (
                            origin.get("verified") is True and wheel and module.startswith(wheel.rstrip("/") + "/")
                            and ".." not in PurePosixPath(wheel).parts and ".." not in PurePosixPath(module).parts
                            and PurePosixPath(wheel).name == record["filename"] and wheel.endswith(".whl")
                            and origin.get("wheel_sha256") == record["sha256"]
                        ):
                            failures.append(f"The {name} import-origin proof is invalid")
                        origins.append(wheel)
                    if len(set(origins)) != 2:
                        failures.append("The direct and sdist-rebuilt oracle origins are not independent")
        except (*_BUNDLE_ERRORS, AttributeError):
            failures.append(f"The {role}'s hosted phase/count/hash proof is missing or unreadable")
        return failures

    def submit_patch(self, changes, *, timeout_seconds):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (float, int)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("The CI timeout must be a finite positive number")
        start = time.monotonic()
        deadline = start + timeout_seconds
        previous_deadline = getattr(self, "_operation_deadline", None)
        self._operation_deadline = deadline
        context = {"phase": "validate-patch", "attempt": self.attempt + 1}
        candidate_sha = branch = None
        target = oracle = None
        updated = None
        try:
            if self.task.oracle_sha is None:
                raise BackendError("Pin and validate the trusted oracle before starting a trial")
            updated = self._check_changes(changes)
            context["phase"] = "check-oracle-pin"
            current_ref = self._api("GET", "/git/ref/heads/" + quote(self.task.oracle_ref, safe=""))
            if _revision(current_ref.get("object")) != self.task.oracle_sha:
                raise BackendError("The trusted verification branch moved during the experiment")
            context["phase"] = "create-candidate"
            base_commit = self._api("GET", "/git/commits/" + self.task.base_sha)
            base_tree = _revision(base_commit.get("tree"))
            tree_entries = []
            for path, content in updated.items():
                if content == self.originals[path]:
                    continue
                blob = self._api("POST", "/git/blobs", {"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"}) if content is not None else None
                tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": _revision(blob) if blob else None})
            tree = self._api("POST", "/git/trees", {"base_tree": base_tree, "tree": tree_entries})
            self.attempt += 1
            commit = self._api("POST", "/git/commits", {"message": f"BuildTrace experimental patch {self.namespace} attempt {self.attempt}", "tree": _revision(tree), "parents": [self.task.base_sha]})
            candidate_sha = _revision(commit)
            branch = f"buildtrace-trials/{self.namespace}-p{self.attempt}"
            context.update(candidate_sha=candidate_sha, branch=branch)
            self._api("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": candidate_sha})
            if not hasattr(self, "_trial_branches"):
                self._trial_branches = set()
            self._trial_branches.add(branch)
            self.overlay = updated
            context["phase"] = "target-ci"
            target = self._dispatch_and_capture(branch, candidate_sha, {}, deadline)
            context["phase"] = "trusted-oracle"
            oracle = self._dispatch_and_capture(self.task.oracle_ref, self.task.oracle_sha, {"source_sha": candidate_sha, "oracle_mode": True}, deadline, expected_title=f"Oracle {candidate_sha}", archive_oracle=True)
            context["phase"] = "validate-protected-evidence"
            oracle_failures = self._oracle_failures(oracle, candidate_sha)
            target_failures = self._hosted_failures(target, candidate_sha, oracle=False) if self._verification_schema() == 2 else []
            elapsed = time.monotonic() - start
            verification = Verification(
                attempt=self.attempt, candidate_sha=candidate_sha, run_url=target["run_url"],
                oracle_run_url=oracle["run_url"], workflow_conclusion=target["run"].get("conclusion") or "unknown",
                required_checks_passed=required_checks_passed(self.task, target) and not target_failures,
                protected_files_unchanged=True, oracle_passed=not oracle_failures,
                elapsed_seconds=elapsed, capture_id=target["id"], oracle_capture_id=oracle["id"],
                details="; ".join(target_failures + oracle_failures),
            )
            self.ci_operation_seconds = getattr(self, "ci_operation_seconds", 0) + elapsed
            self.last_failure = None
            return PatchResult(verification, changed_hunks(self.originals, self.overlay), {
                "verification": verification.model_dump(mode="json"),
                "current_logs": self._log_catalog(target), "oracle_logs": self._log_catalog(oracle),
                "diagnostics": {"target_warnings": target.get("warnings", []), "oracle_warnings": oracle.get("warnings", [])},
            })
        except (BackendError, EvidenceError, GitHubError, httpx.RequestError, OSError) as exc:
            error = _infrastructure_error(exc)
            states = [state for state in getattr(self, "_dispatches", {}).values() if state["attempt"] == context["attempt"]]
            if time.monotonic() >= deadline:
                for state in states:
                    if not state.get("capture_id") and "cancellation" not in state:
                        state["cancellation"] = self._cancel_overdue_dispatch(state)
            for state in states:
                if state["capture_id"]:
                    try:
                        capture = self.store.load(state["capture_id"])
                        if state["kind"] == "target":
                            target = capture
                        else:
                            oracle = capture
                    except (EvidenceError, OSError):
                        pass
            elapsed = time.monotonic() - start
            error.elapsed_seconds = elapsed
            error.diagnostics = context | error.diagnostics | {
                "dispatches": [self._dispatch_diagnostics(state) for state in states],
                "target_capture_id": target["id"] if target else None,
                "oracle_capture_id": oracle["id"] if oracle else None,
            }
            target_runs = [run for state in states if state["kind"] == "target" for run in state["runs"]]
            oracle_runs = [run for state in states if state["kind"] == "oracle" for run in state["runs"]]
            target_url = target["run_url"] if target else target_runs[0].get("html_url") if len(target_runs) == 1 else None
            oracle_url = oracle["run_url"] if oracle else oracle_runs[0].get("html_url") if len(oracle_runs) == 1 else None
            if candidate_sha is not None and target_url is not None:
                error.verification = Verification(
                    attempt=self.attempt, candidate_sha=candidate_sha, run_url=target_url,
                    oracle_run_url=oracle_url, workflow_conclusion=(target["run"].get("conclusion") or "unknown") if target else "unknown",
                    required_checks_passed=required_checks_passed(self.task, target) if target else False,
                    protected_files_unchanged=True, oracle_passed=False, elapsed_seconds=elapsed,
                    capture_id=target["id"] if target else None, oracle_capture_id=oracle["id"] if oracle else None,
                    details=f"Infrastructure failure during {context['phase']}: {error}",
                )
            if candidate_sha is not None and updated is not None:
                error.hunks = changed_hunks(self.originals, updated)
            error.observation = {
                "infrastructure_error": error.as_dict(),
                "current_logs": self._log_catalog(target) if target else [],
                "oracle_logs": self._log_catalog(oracle) if oracle else [],
            }
            self.ci_operation_seconds = getattr(self, "ci_operation_seconds", 0) + elapsed
            self.last_failure = error
            if error is exc:
                raise
            raise error from exc
        finally:
            self._operation_deadline = previous_deadline
