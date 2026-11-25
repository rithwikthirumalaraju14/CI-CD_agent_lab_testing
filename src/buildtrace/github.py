"""Read GitHub Actions evidence; preserve the selected run attempt."""

import base64
import binascii
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Callable, TypeVar
from urllib.parse import quote, urlsplit

import httpx

from buildtrace.models import ApiDocument, CollectedRun, RunReference, SourceFile

API_ROOT = "https://api.github.com"
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
SOURCE_PATHS = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-ci.txt",
    "ci/constraints.txt",
    ".python-version",
)
_T = TypeVar("_T")
_TRANSIENT_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class GetRetryPolicy:
    """A common bounded transport policy, never a policy for retrying repairs."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    max_wait_seconds: float = 5.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError("GET retries need between one and ten attempts")
        delays = (self.base_delay_seconds, self.max_delay_seconds, self.max_wait_seconds)
        if any(not math.isfinite(value) or value < 0 for value in delays):
            raise ValueError("GET retry delays must be finite and nonnegative")


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def retry_github_get(operation: Callable[[], _T], *, policy: GetRetryPolicy | None = None, deadline: float | None = None) -> _T:
    """Run one idempotent GET, including reading its body, with bounded retries.

    Callers must not put a mutation inside operation. A Retry-After longer than
    the common wait/deadline budget ends the operation rather than retrying early.
    Invalid input, response formats, permissions, and unavailable files are not
    transient failures.
    """
    policy = policy or GetRetryPolicy()
    waited = 0.0
    for attempt in range(1, policy.max_attempts + 1):
        if deadline is not None and time.monotonic() >= deadline:
            raise GitHubError("The GitHub GET deadline was reached.")
        try:
            return operation()
        except (*_TRANSIENT_TRANSPORT_ERRORS, GitHubError) as exc:
            exc.retry_attempts = attempt
            transient = not isinstance(exc, GitHubError) or exc.status_code == 429 or (
                exc.status_code is not None and 500 <= exc.status_code < 600
            )
            if not transient or attempt == policy.max_attempts:
                raise
            delay = min(policy.max_delay_seconds, policy.base_delay_seconds * 2 ** (attempt - 1))
            requested = getattr(exc, "retry_after_seconds", None)
            if requested is not None:
                delay = max(delay, requested)
            if waited + delay > policy.max_wait_seconds:
                raise
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise
            time.sleep(delay)
            waited += delay
    raise AssertionError("Unreachable GET retry state")


def github_timeout(deadline: float | None = None) -> httpx.Timeout:
    remaining = 20.0 if deadline is None else min(20.0, max(0.001, deadline - time.monotonic()))
    return httpx.Timeout(remaining, connect=min(10.0, remaining))


def resolve_github_token() -> str | None:
    """Use an environment token, gh sign-in, or an explicitly selected Git credential account."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if token := os.environ.get(name, "").strip():
            return token
    if executable := shutil.which("gh"):
        try:
            result = subprocess.run(
                [executable, "auth", "token", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                timeout=5,
                env=os.environ | {"GH_PROMPT_DISABLED": "1"},
            )
            if result.returncode == 0:
                return result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    account = os.environ.get("BUILDTRACE_GITHUB_ACCOUNT", "")
    git = shutil.which("git")
    if account and git and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", account):
        try:
            result = subprocess.run(
                [git, "credential", "fill"],
                input=f"protocol=https\nhost=github.com\nusername={account}\n\n",
                capture_output=True, text=True, timeout=10,
                env=os.environ | {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            )
            if result.returncode == 0:
                fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                return fields.get("password") or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


class GitHubActionsClient:
    def __init__(self, token: str | None = None, client: httpx.Client | None = None, *, retry_policy: GetRetryPolicy | None = None, deadline: float | None = None):
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(20, connect=10))
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "BuildTrace/0.1.0",
        }
        token = resolve_github_token() if token is None else token
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._documents: list[ApiDocument] = []
        self.collection_errors: dict[str, dict] = {}
        self.retry_policy = retry_policy or GetRetryPolicy()
        self.deadline = deadline

    def __enter__(self) -> "GitHubActionsClient":
        return self

    def __exit__(self, *_args) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 401:
            message = "GitHub rejected the credentials. Sign in with gh or configure GH_TOKEN."
        elif status in {403, 429} and response.headers.get("x-ratelimit-remaining") == "0":
            message = "GitHub's API rate limit was reached. Configure authentication or retry after the reset."
        elif status == 403:
            message = "GitHub denied access. Check repository access and Actions read permission."
        elif status == 429:
            message = "GitHub's API rate limit was reached. Retry after the requested delay."
        elif status == 404:
            message = "GitHub could not find the requested run or file, or the account cannot access it."
        elif status in {409, 410}:
            message = "GitHub's execution logs are not available yet or have expired."
        elif status >= 500:
            message = "GitHub is temporarily unavailable. Please retry."
        else:
            message = f"GitHub returned HTTP {status}."
        raise GitHubError(message, status, retry_after_seconds=_retry_after(response))

    def _get_json(self, path: str) -> dict:
        def get():
            response = self._client.get(API_ROOT + path, headers=self._headers, follow_redirects=False, timeout=github_timeout(self.deadline))
            try:
                self._check_status(response)
                return response, response.json()
            finally:
                response.close()

        try:
            response, value = retry_github_get(get, policy=self.retry_policy, deadline=self.deadline)
        except httpx.RequestError as exc:
            error = GitHubError("Could not reach GitHub. Check the connection and retry.")
            error.retry_attempts = getattr(exc, "retry_attempts", 1)
            raise error from exc
        except ValueError as exc:
            raise GitHubError("GitHub returned an invalid JSON response.") from exc
        if not isinstance(value, dict):
            raise GitHubError("GitHub returned an unexpected response format.")
        self._documents.append(ApiDocument(endpoint=path, content=response.content))
        return value

    def _list(self, path: str, key: str) -> list[dict]:
        records = []
        for page in range(1, 51):
            result = self._get_json(f"{path}?per_page=100&page={page}")
            batch = result.get(key)
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise GitHubError(f"GitHub returned an invalid {key} list.")
            records.extend(batch)
            if len(batch) < 100:
                return records
        raise GitHubError(f"This run exceeds the viewer's 5,000-item {key} limit.")

    @staticmethod
    def _validate_download_url(url: str) -> None:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        allowed = (
            host == "api.github.com"
            or host.endswith(".blob.core.windows.net")
            or host.endswith(".githubusercontent.com")
        )
        if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
            raise GitHubError("GitHub returned an unsupported log download location.")
        try:
            if parsed.port not in {None, 443}:
                raise GitHubError("GitHub returned an unsupported log download port.")
        except ValueError as exc:
            raise GitHubError("GitHub returned an invalid log download location.") from exc

    def _download_logs(self, path: str) -> bytes:
        try:
            return retry_github_get(lambda: self._download_logs_once(path), policy=self.retry_policy, deadline=self.deadline)
        except httpx.RequestError as exc:
            error = GitHubError("The log download was interrupted. Please retry.")
            error.retry_attempts = getattr(exc, "retry_attempts", 1)
            raise error from exc

    def _download_logs_once(self, path: str) -> bytes:
        url = API_ROOT + path
        headers = self._headers.copy()
        for _ in range(4):
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise GitHubError("The log download deadline was reached.")
            request = self._client.build_request("GET", url, headers=headers, timeout=github_timeout(self.deadline))
            # A client's default headers must not leak credentials to signed storage URLs.
            if not url.startswith(API_ROOT + "/"):
                request.headers.pop("Authorization", None)
            response = self._client.send(request, stream=True, follow_redirects=False, auth=None)
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = response.headers.get("location", "")
                    self._validate_download_url(url)
                    headers = {"User-Agent": self._headers["User-Agent"]}
                    continue
                self._check_status(response)
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    if self.deadline is not None and time.monotonic() >= self.deadline:
                        raise GitHubError("The log download deadline was reached.")
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise GitHubError("The log archive exceeds the viewer's 20 MiB download limit.")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()
        raise GitHubError("GitHub returned too many log download redirects.")

    def _get_source(self, reference: RunReference, path: str, sha: str) -> SourceFile | None:
        endpoint = f"/repos/{reference.full_name}/contents/{quote(path, safe='/')}?ref={sha}"
        try:
            result = self._get_json(endpoint)
        except GitHubError as exc:
            if exc.status_code == 404:
                return None
            raise
        if result.get("type") != "file" or result.get("encoding") != "base64":
            raise GitHubError(f"Could not archive {path}: unsupported file representation.")
        if type(result.get("size", 0)) is not int or not isinstance(result.get("content"), str):
            raise GitHubError(f"Could not archive {path}: invalid source-file metadata.")
        if result.get("size", 0) > MAX_SOURCE_BYTES:
            raise GitHubError(f"Could not archive {path}: source file exceeds 1 MiB.")
        try:
            encoded = "".join(result["content"].split())
            content = base64.b64decode(encoded, validate=True)
        except (KeyError, ValueError, TypeError, binascii.Error) as exc:
            raise GitHubError(f"Could not decode the source file {path}.") from exc
        if len(content) > MAX_SOURCE_BYTES:
            raise GitHubError(f"Could not archive {path}: source file exceeds 1 MiB.")
        return SourceFile(path=path, content=content)

    def collect(self, reference: RunReference) -> CollectedRun:
        self._documents = []
        self.collection_errors = {}
        latest = self._get_json(reference.api_base)
        attempt = reference.attempt or latest.get("run_attempt", 1)
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise GitHubError("GitHub returned an invalid run attempt.")
        reference = replace(reference, attempt=attempt)
        attempt_base = f"{reference.api_base}/attempts/{attempt}"
        run = self._get_json(attempt_base)
        if run.get("id") != reference.run_id or run.get("run_attempt", attempt) != attempt:
            raise GitHubError("GitHub returned a different workflow run attempt than requested.")
        sha = run.get("head_sha", "")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
            raise GitHubError("GitHub returned an invalid source revision.")
        capture = CollectedRun(
            reference=reference,
            run=run,
            jobs=self._list(attempt_base + "/jobs", "jobs"),
            source_ref=sha,
        )
        if run.get("event") in {"pull_request", "pull_request_target"}:
            capture.warnings.append(
                "Pull-request workflows can check out a merge revision. Source files below use "
                "the reported head SHA; inspect checkout evidence before treating it as the tested revision."
            )
        try:
            capture.log_archive = self._download_logs(attempt_base + "/logs")
        except GitHubError as exc:
            capture.warnings.append(f"Log archive unavailable: {exc}")
        try:
            capture.artifacts = self._list(reference.api_base + "/artifacts", "artifacts")
        except GitHubError as exc:
            capture.warnings.append(f"Artifact listing unavailable: {exc}")
            self.collection_errors["artifacts"] = {
                "message": str(exc), "status_code": exc.status_code,
                "request_attempts": getattr(exc, "retry_attempts", 1),
            }

        workflow_path = str(run.get("path", "")).split("@", 1)[0]
        workflow = PurePosixPath(workflow_path)
        paths = list(SOURCE_PATHS)
        if workflow.parent == PurePosixPath(".github/workflows") and workflow.suffix in {".yml", ".yaml"}:
            paths.insert(0, workflow_path)
        else:
            capture.warnings.append("The workflow path could not be resolved to a repository YAML file.")
        for path in paths:
            try:
                if source := self._get_source(reference, path, sha):
                    capture.sources.append(source)
            except GitHubError as exc:
                capture.warnings.append(str(exc))
        capture.api_documents = self._documents.copy()
        return capture
