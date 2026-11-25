"""Operator-only, foreground checkpoints for hosted benchmark validation.

Local preparation/specification never authenticate. Publishing and reference
smokes require explicit authorization. Only public/ and trusted/ are deploy
trees; every journal, reference repair and capture remains outside both.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import fnmatch
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from typing import Callable
from urllib.parse import quote, urlsplit
import uuid
import zipfile

import httpx

from buildtrace.evidence import EvidenceError, EvidenceStore
from buildtrace.experiments.backend import BackendError, GitHubTrialBackend, safe_repository_path
from buildtrace.experiments.models import (
    Budget, ExperimentSpec, ModelSettings, RetrievalSettings, TaskIdentity, TaskSpec, fingerprint,
)
from buildtrace.experiments.research import create_split_registry
from buildtrace.github import (
    API_ROOT, GetRetryPolicy, GitHubActionsClient, GitHubError, github_timeout,
    resolve_github_token, retry_github_get,
)
from buildtrace.models import RunReference, SourceFile

from .catalogue import CASES, HARD_DEVELOPMENT_CASES, HARD_TEST_CASES
from .materialize import is_editable, materialize_case, reference_edits, tree_sha256


SCHEMA = "buildtrace.research-operator.v1"
DEFAULT_ROOT = Path("data/research-v2")
DEFAULT_REPOSITORY = "bluepal-rithwik-thirumalaraju/buildtrace-ci-lab"
_REVISION = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")
_TREE_NAMES = ("raw", "bundle", "public", "trusted")
_HOSTED_FILES = ("prepare_hosted.py", "hosted_verify.py", "hosted-ci.yml")
_SHARED = (".github/workflows/ci.yml", ".github/research_v2/verify.py", ".github/research_v2/public.json")
_CONTRACT = "infra/research_v2/contract.json"
_PROJECT_STEPS = frozenset({
    "Resolve offline project", "Run protected tests", "Build distributions", "Run example", "Check installed distributions",
})
_BOOTSTRAP_STEPS = frozenset({
    "Check out pinned verifier revision", "Check out candidate revision", "Set up Python",
    "Provision pinned verification tools", "Record candidate and protected inputs",
})


class OperatorError(RuntimeError):
    """Only controlled, credential-free summaries belong in this exception."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details

    def as_dict(self):
        return {"message": str(self), **self.details}


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seal(kind: str, **value) -> dict:
    document = {"schema_version": SCHEMA, "kind": kind, **value}
    return {**document, "sha256": _digest(_canonical(document))}


def _read_json(path: Path):
    try:
        return json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise OperatorError("An operator JSON checkpoint is unreadable", file=path.name) from None


def _read_sealed(path: Path, kind: str) -> dict:
    document = _read_json(path)
    if not isinstance(document, dict):
        raise OperatorError("Invalid operator checkpoint", file=path.name)
    body = {key: value for key, value in document.items() if key != "sha256"}
    if document.get("schema_version") != SCHEMA or document.get("kind") != kind or document.get("sha256") != _digest(_canonical(body)):
        raise OperatorError("Operator checkpoint hash/kind differs", file=path.name)
    return document


def _write_once(path: Path, value, *, identical_ok: bool = True) -> None:
    """Publish an immutable JSON file atomically, with no replacement operation."""
    data = _canonical(value) + b"\n"
    if any(parent.is_symlink() for parent in path.parents):
        raise OperatorError("Refusing a symlink checkpoint parent")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OperatorError("Refusing a symlink checkpoint", file=path.name)
    if path.exists():
        if identical_ok and path.is_file() and path.read_bytes() == data:
            return
        raise OperatorError("Refusing to overwrite an existing operator file", file=path.name)
    temporary = path.parent / (".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard link publishes complete bytes and refuses an existing name
            # on both NTFS and POSIX; rename/replace can clobber on POSIX.
            os.link(temporary, path)
        except FileExistsError:
            if not identical_ok or path.is_symlink() or path.read_bytes() != data:
                raise OperatorError("Concurrent checkpoint conflict; no file was replaced", file=path.name) from None
    finally:
        temporary.unlink(missing_ok=True)


class Journal:
    """Hash-chained immutable events; interrupted mutations retain their intent."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.events = []
        previous = None
        if directory.is_symlink():
            raise OperatorError("Refusing a symlink journal")
        for number, path in enumerate(sorted(directory.glob("*.json")), 1):
            event = _read_sealed(path, "checkpoint")
            if path.name != f"{number:06d}.json" or event.get("sequence") != number or event.get("previous_sha256") != previous:
                raise OperatorError("The operator journal chain is incomplete or changed")
            self.events.append(event)
            previous = event["sha256"]

    def record(self, event: str, key: str, data: dict) -> dict:
        document = _seal(
            "checkpoint", sequence=len(self.events) + 1,
            previous_sha256=self.events[-1]["sha256"] if self.events else None,
            event=event, key=key, data=data,
        )
        _write_once(self.directory / f"{len(self.events) + 1:06d}.json", document, identical_ok=False)
        self.events.append(document)
        return document

    def last(self, event: str, key: str | None = None) -> dict | None:
        return next((row["data"] for row in reversed(self.events) if row["event"] == event and (key is None or row["key"] == key)), None)

    def pending_mutation(self, key: str) -> bool:
        rows = [row for row in self.events if row["key"] == key and row["event"] in {"mutation-intent", "mutation-error", "object-confirmed"}]
        if not rows or rows[-1]["event"] == "object-confirmed":
            return False
        # A definite HTTP rejection may be retried by a later foreground
        # invocation. An ambiguous response is reconciled by GET only.
        return rows[-1]["event"] == "mutation-intent" or rows[-1]["data"].get("ambiguous_mutation", True)


def normalize_split(value: str) -> str:
    value = "dev" if value == "development" else value
    if value not in {"train", "dev", "test"}:
        raise OperatorError("Choose train, dev, or test explicitly")
    return value


def select_cases(split: str, case_ids=()):
    split = normalize_split(split)
    # Keep the original default v2 batches stable. Additional difficulty probes
    # must be selected by explicit case ID and use a separate operator root.
    pool = (*CASES, *HARD_DEVELOPMENT_CASES, *HARD_TEST_CASES) if case_ids else CASES
    selected = [case for case in pool if normalize_split(case.split) == split]
    if case_ids:
        requested = set(case_ids)
        if not requested <= {case.id for case in selected}:
            raise OperatorError("Every --case must belong to the active split")
        selected = [case for case in selected if case.id in requested]
    return selected


def _repository(value: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value) or value.split("/")[-1] in {".", ".."}:
        raise OperatorError("Use an OWNER/REPOSITORY name")
    return value.lower()


def _read_tree(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise OperatorError("A prepared tree is missing or is a symlink", tree=root.name)
    files = {}
    for path in sorted(root.rglob("*")):
        name = safe_repository_path(path.relative_to(root).as_posix())
        if any(part.lower().startswith(".env") for part in PurePosixPath(name).parts):
            raise OperatorError("Unexpected environment file in an operator tree")
        if path.is_symlink():
            raise OperatorError("Prepared trees may contain only regular files/directories")
        if path.is_file():
            files[name] = path.read_bytes()
    return files


def git_object_sha(kind: str, data: bytes) -> str:
    return hashlib.sha1(kind.encode("ascii") + b" " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def git_tree_sha(files: dict[str, bytes]) -> str:
    root = {}
    for name, content in files.items():
        parts = PurePosixPath(safe_repository_path(name)).parts
        current = root
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = git_object_sha("blob", content)

    def encode(node):
        content = bytearray()
        for name, value in sorted(node.items(), key=lambda pair: (pair[0] + ("/" if isinstance(pair[1], dict) else "")).encode("utf-8")):
            directory = isinstance(value, dict)
            sha = encode(value) if directory else value
            content.extend(("40000" if directory else "100644").encode() + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(sha))
        return git_object_sha("tree", bytes(content))

    return encode(root)


def _tree_record(files: dict[str, bytes]) -> dict:
    return {"sha256": tree_sha256(files), "git_tree_sha": git_tree_sha(files), "files": {name: _digest(data) for name, data in sorted(files.items())}}


def _load_preparer():
    directory = Path(__file__).resolve().parents[3] / "infra/research_v2"
    path = directory / "prepare_hosted.py"
    spec = importlib.util.spec_from_file_location("_research_operator_prepare", path)
    if spec is None or spec.loader is None:
        raise OperatorError("Could not load the hosted preparer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources = {name: _digest((directory / name).read_bytes()) for name in _HOSTED_FILES}
    return module, sources


def _check_separation(trees: dict, fields: dict) -> None:
    raw, public, trusted = (trees[name] for name in ("raw", "public", "trusted"))
    if set(public) != set(raw) | set(_SHARED):
        raise OperatorError("Hosted public output has an unexpected file set")
    contract_bytes = trusted.get(_CONTRACT, b"")
    if _digest(contract_bytes) != fields.get("verification_contract_sha256"):
        raise OperatorError("Hosted contract bytes differ from their task metadata")
    contract = json.loads(contract_bytes)
    oracle_names = {"infra/research_v2/oracle/" + name for name in contract["oracle_sha256"]}
    if set(trusted) != set(_SHARED) | {_CONTRACT} | oracle_names or any(trusted[name] != public[name] for name in _SHARED):
        raise OperatorError("Hosted trusted output has an unexpected file set")
    descriptor = json.loads(public[_SHARED[2]])
    if set(descriptor) != {"schema_version", "expected_public_tests"}:
        raise OperatorError("Operator metadata leaked into the public descriptor")
    if fields.get("schema_version") != 2 or fields.get("artifact_prefix") != "bench-ci" or fields.get("editable_environment_keys") != []:
        raise OperatorError("The hosted preparer did not return the strict generic task contract")
    if fields["expected_test_count"] != contract["expected_public_tests"] or fields["expected_oracle_test_count"] != contract["expected_oracle_tests"]:
        raise OperatorError("Separate hosted test counts differ")


def prepare_case(root: Path | str, case, repository: str = DEFAULT_REPOSITORY) -> dict:
    # Scanner/file-indexer locks can affect any staged leaf or TemporaryDirectory
    # cleanup on Windows. Reconcile the identical allowlisted case on retry;
    # existing complete leaves/metadata are hash-checked, never replaced.
    delays = (0.05, 0.1, 0.2, 0.4)
    for attempt in range(len(delays) + 1):
        try:
            return _prepare_case_once(root, case, repository)
        except PermissionError:
            if attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def _prepare_case_once(root: Path | str, case, repository: str = DEFAULT_REPOSITORY) -> dict:
    root, repository = Path(root).resolve(), _repository(repository)
    home = root / "cases" / case.id
    if (root / "cases").is_symlink() or home.is_symlink():
        raise OperatorError("Refusing a symlink case directory")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root) as directory:
        stage = Path(directory)
        generated = materialize_case(case.id, stage / "raw", verifier_dir=stage / "bundle")
        preparer, sources = _load_preparer()
        fields = preparer.prepare_deployment(stage / "raw", stage / "bundle", stage / "public", stage / "trusted")
        trees = {name: _read_tree(stage / name) for name in _TREE_NAMES}
        # Generator manifests are operator-side. Normalize their absolute paths
        # before copying to new files, so resumption has no random staging paths.
        manifest = json.loads(trees["bundle"]["manifest.json"])
        manifest.update(destination=str(home / "raw"), verifier_dir=str(home / "bundle"))
        trees["bundle"]["manifest.json"] = _canonical(manifest) + b"\n"
        _check_separation(trees, fields)
        records = {name: _tree_record(files) for name, files in trees.items()}
        token = _digest(_canonical({"repository": repository, "public": records["public"]["git_tree_sha"], "trusted": records["trusted"]["git_tree_sha"], "schema_version": SCHEMA}))[:24]
        prepared = _seal(
            "prepared", case_id=case.id, split=normalize_split(case.split), family=case.family,
            repository=repository, trees=records, hosted_fields=fields, hosted_sources_sha256=sources,
            refs={"input": "buildtrace-inputs/rv2-" + token, "oracle": "buildtrace-oracles/rv2-" + token},
            snapshot_sha256=generated.contract.snapshot_sha256,
        )
        marker = home / "prepared.json"
        if marker.exists() and _read_sealed(marker, "prepared") != prepared:
            raise OperatorError("Prepared identity/templates changed; use a new operator root")
        # Validate every existing leaf before creating any missing leaf.
        for name in _TREE_NAMES:
            destination = home / name
            if destination.exists() or destination.is_symlink():
                if _tree_record(_read_tree(destination)) != records[name]:
                    raise OperatorError("Existing prepared tree differs; no files were overwritten", tree=name)
        home.mkdir(parents=True, exist_ok=True)
        for name, files in trees.items():
            destination = home / name
            if destination.exists():
                continue
            export = stage / ("export-" + name)
            export.mkdir()
            for relative, content in files.items():
                path = export.joinpath(*PurePosixPath(relative).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(content)
            if destination.exists() or destination.is_symlink():
                raise OperatorError("Concurrent preparation; no destination was replaced")
            export.rename(destination)
        _write_once(marker, prepared)
        return prepared


def load_prepared(root: Path | str, case, repository: str) -> dict:
    home = Path(root).resolve() / "cases" / case.id
    if home.is_symlink() or home.parent.is_symlink():
        raise OperatorError("Refusing a symlink case directory")
    document = _read_sealed(home / "prepared.json", "prepared")
    if any(document.get(key) != value for key, value in {
        "case_id": case.id, "split": normalize_split(case.split), "family": case.family, "repository": _repository(repository),
    }.items()):
        raise OperatorError("Prepared metadata belongs to another case/split/repository")
    for name in _TREE_NAMES:
        if _tree_record(_read_tree(home / name)) != document["trees"][name]:
            raise OperatorError("Prepared tree hash differs", tree=name)
    return document


class MutationClient:
    """Share one throttle with Git data publication and GitHubTrialBackend."""

    def __init__(self, client, *, interval: float = 1.05, clock=time.monotonic, sleep=time.sleep):
        self.client, self.interval, self.clock, self.sleep = client, interval, clock, sleep
        self.last_mutation = None
        self.on_mutation: Callable | None = None

    def request(self, method, url, **kwargs):
        if method.upper() in {"GET", "HEAD"}:
            return self.client.request(method, url, **kwargs)
        if self.last_mutation is not None:
            self.sleep(max(0, self.interval - (self.clock() - self.last_mutation)))
        self.last_mutation = self.clock()
        info = {"method": method.upper(), "path": urlsplit(str(url)).path, "payload_sha256": _digest(_canonical(kwargs.get("json")))}
        if self.on_mutation:
            self.on_mutation("http-intent", info)
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            if self.on_mutation:
                self.on_mutation("http-error", {**info, "error_type": type(exc).__name__})
            raise
        if self.on_mutation:
            observed = {**info, "status_code": response.status_code}
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("sha"), str) and _REVISION.fullmatch(body["sha"]):
                    observed["sha"] = body["sha"]
            except ValueError:
                pass
            self.on_mutation("http-response", observed)
        return response

    def __getattr__(self, name):
        return getattr(self.client, name)


class GitHubOperatorAPI:
    def __init__(self, repository: str, *, token: str | None = None, client=None, retry_policy=None, clock=time.monotonic, sleep=time.sleep):
        self.repository = _repository(repository)
        self.token = resolve_github_token() if token is None else token
        if not self.token:
            raise OperatorError("Configure GitHub authentication before this command")
        self.retry_policy = retry_policy or GetRetryPolicy()
        self._owns_client = client is None
        self.client = MutationClient(client or httpx.Client(timeout=20), clock=clock, sleep=sleep)
        self.headers = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if self._owns_client:
            self.client.close()

    def request(self, method: str, path: str, payload=None, *, missing_ok=False, deadline=None):
        def send():
            response = self.client.request(method, API_ROOT + "/repos/" + self.repository + path, headers=self.headers, json=payload, timeout=github_timeout(deadline), follow_redirects=False)
            try:
                if method == "GET" and missing_ok and response.status_code == 404:
                    return None
                GitHubActionsClient._check_status(response)
                document = response.json() if response.content else {}
                if not isinstance(document, dict):
                    raise ValueError("Expected a GitHub object")
                return document
            finally:
                response.close()

        try:
            return retry_github_get(send, policy=self.retry_policy, deadline=deadline) if method == "GET" else send()
        except (httpx.RequestError, GitHubError, ValueError) as exc:
            status = getattr(exc, "status_code", None)
            # No upstream exception text, request headers, URLs with query
            # credentials, or response bodies are interpolated or serialized.
            raise OperatorError(
                "GitHub operator request failed", method=method, path=path.split("?")[0],
                status_code=status, error_type=type(exc).__name__, request_attempts=getattr(exc, "retry_attempts", 1),
                ambiguous_mutation=method != "GET" and (isinstance(exc, (httpx.RequestError, ValueError)) or status is not None and status >= 500),
            ) from None

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload):
        return self.request("POST", path, payload)


def _revision(value) -> str:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise OperatorError("GitHub returned an invalid Git object identity")
    return value


def _check_remote_tree(api, sha: str, files: dict[str, bytes]) -> None:
    document = api.get("/git/trees/" + sha + "?recursive=1")
    if document.get("sha") != sha or document.get("truncated") is not False or not isinstance(document.get("tree"), list):
        raise OperatorError("Remote full tree cannot be validated")
    expected = {name: git_object_sha("blob", data) for name, data in files.items()}
    observed = {}
    for entry in document["tree"]:
        if not isinstance(entry, dict):
            raise OperatorError("Remote Git tree has an invalid entry")
        if entry.get("type") == "tree" and entry.get("mode") == "040000":
            continue
        if entry.get("type") != "blob" or entry.get("mode") != "100644" or entry.get("path") in observed:
            raise OperatorError("Remote tree is not the exact regular-file snapshot")
        observed[entry.get("path")] = entry.get("sha")
    if observed != expected or sha != git_tree_sha(files):
        raise OperatorError("An existing ref/tree differs from the intended snapshot; refs are never moved")


def _remote_ref(api, ref: str, files: dict[str, bytes], *, pinned: str | None = None) -> str | None:
    body = api.get("/git/ref/heads/" + quote(ref, safe=""), missing_ok=True)
    if body is None:
        if pinned:
            raise OperatorError("An immutable published ref disappeared")
        return None
    if body.get("ref") != "refs/heads/" + ref or body.get("object", {}).get("type") != "commit":
        raise OperatorError("GitHub returned a different ref/object type")
    sha = _revision(body["object"].get("sha"))
    if pinned and sha != pinned:
        raise OperatorError("An immutable published ref moved")
    commit = api.get("/git/commits/" + sha)
    if commit.get("sha") != sha or commit.get("parents") != [] or commit.get("tree", {}).get("sha") != git_tree_sha(files):
        raise OperatorError("An existing ref is not the intended orphan snapshot")
    _check_remote_tree(api, commit["tree"]["sha"], files)
    return sha


def _ensure_object(api, journal: Journal, key: str, path: str, payload: dict, lookup: Callable, validate: Callable):
    found = lookup()
    if found is not None:
        result = validate(found)
        journal.record("object-confirmed", key, {"sha": result, "reconciled": True})
        return result
    if journal.pending_mutation(key):
        raise OperatorError("A previous mutation is unresolved; GET reconciliation found no object and POST was not repeated", operation=key, ambiguous_mutation=True)
    journal.record("mutation-intent", key, {"method": "POST", "path": path, "payload_sha256": _digest(_canonical(payload))})
    try:
        body = api.post(path, payload)
        result = validate(body)
    except OperatorError as exc:
        journal.record("mutation-error", key, exc.as_dict())
        found = lookup()
        if found is None:
            raise
        result = validate(found)
    journal.record("object-confirmed", key, {"sha": result})
    return result


def _blob(api, root: Path, journal: Journal, data: bytes) -> str:
    sha = git_object_sha("blob", data)
    record = _seal("blob", repository=api.repository, git_sha=sha, content_sha256=_digest(data), byte_count=len(data))
    cache = root / "blob-cache" / _digest(api.repository.encode())[:20] / (sha + ".json")
    if cache.exists():
        if _read_sealed(cache, "blob") != record:
            raise OperatorError("A cached blob identity differs")
        return sha

    def validate(body):
        if body.get("sha") != sha:
            raise OperatorError("GitHub blob identity differs from exact local bytes", ambiguous_mutation=True)
        # POST responses have no content. GET responses must verify it too.
        if "content" in body:
            try:
                content = base64.b64decode("".join(body["content"].split()), validate=True)
            except (ValueError, TypeError, AttributeError):
                raise OperatorError("Invalid GitHub blob encoding") from None
            if body.get("encoding") != "base64" or content != data:
                raise OperatorError("GitHub blob content differs from exact local bytes")
        return sha

    _ensure_object(api, journal, "blob-" + sha, "/git/blobs", {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}, lambda: api.get("/git/blobs/" + sha, missing_ok=True), validate)
    _write_once(cache, record)
    return sha


def _commit_plan(tree: str, opaque: str) -> tuple[dict, str]:
    message = "Benchmark snapshot " + opaque + "\n"
    person = {"name": "BuildTrace Benchmark Operator", "email": "buildtrace-benchmark@users.noreply.github.com", "date": "2024-01-01T00:00:00Z"}
    line = "BuildTrace Benchmark Operator <buildtrace-benchmark@users.noreply.github.com> 1704067200 +0000"
    raw = f"tree {tree}\nauthor {line}\ncommitter {line}\n\n{message}".encode("utf-8")
    return {"message": message, "tree": tree, "parents": [], "author": person, "committer": person}, git_object_sha("commit", raw)


def _identities(root: Path) -> list[dict]:
    result = []
    for path in sorted((root / "identities").glob("*.json")):
        row = _read_sealed(path, "registered-identity")
        identity = TaskIdentity.model_validate(row["identity"]).model_dump(mode="json")
        if path.stem != identity["id"]:
            raise OperatorError("Published registry identity/path differs")
        result.append(identity)
    return result


def _registry(root: Path, additions=()) -> dict:
    registry = create_split_registry(additions, _identities(root))
    _write_once(root / "registries" / (registry["sha256"] + ".json"), registry)
    return registry


def _register(root: Path, prepared: dict, base_sha: str) -> None:
    identity = {key: prepared[key] for key in ("family", "split", "repository")}
    identity.update(id=prepared["case_id"], base_sha=base_sha)
    _registry(root, [identity])  # Check all cross-split identities before publication to disk.
    _write_once(root / "identities" / (identity["id"] + ".json"), _seal("registered-identity", identity=identity))


def publish_case(root: Path | str, case, api, *, allow_publish: bool = False) -> dict:
    if not allow_publish:
        raise OperatorError("Publishing requires --allow-publish")
    root = Path(root).resolve()
    prepared = load_prepared(root, case, api.repository)
    home = root / "cases" / case.id
    journal = Journal(home / "journal")
    files = {role: _read_tree(home / name) for role, name in (("input", "public"), ("oracle", "trusted"))}
    existing = {}
    # A mismatched existing ref is rejected before sending any mutation.
    for role in ("oracle", "input"):
        prior = journal.last("published", role)
        existing[role] = _remote_ref(api, prepared["refs"][role], files[role], pinned=prior["commit_sha"] if prior else None)
    if existing["input"] is not None:
        _register(root, prepared, existing["input"])
    published = {}
    for role in ("oracle", "input"):
        ref, contents = prepared["refs"][role], files[role]
        tree_sha = git_tree_sha(contents)
        commit_sha = existing[role]
        if commit_sha is None:
            entries = [{"path": name, "mode": "100644", "type": "blob", "sha": _blob(api, root, journal, data)} for name, data in sorted(contents.items())]

            def validate_tree(body):
                if body.get("sha") != tree_sha:
                    raise OperatorError("Created full-tree identity differs", ambiguous_mutation=True)
                return tree_sha

            _ensure_object(api, journal, role + "-tree", "/git/trees", {"tree": entries}, lambda: api.get("/git/trees/" + tree_sha, missing_ok=True), validate_tree)
            payload, intended_commit = _commit_plan(tree_sha, ref.rsplit("/", 1)[1])

            def validate_commit(body):
                if body.get("sha") != intended_commit or body.get("tree", {}).get("sha") != tree_sha or body.get("parents") != []:
                    raise OperatorError("Created orphan-commit identity differs", ambiguous_mutation=True)
                return intended_commit

            commit_sha = _ensure_object(api, journal, role + "-commit", "/git/commits", payload, lambda: api.get("/git/commits/" + intended_commit, missing_ok=True), validate_commit)
            if role == "input":
                # Register the real, confirmed Git object before exposing its
                # ref. A process interruption after GitHub accepts the ref must
                # not make this identity disappear from another split's spec.
                # The registry conservatively retains pending ref publications.
                _register(root, prepared, commit_sha)

            def validate_ref(body):
                sha = _revision(body.get("object", {}).get("sha"))
                if body.get("ref") != "refs/heads/" + ref or body.get("object", {}).get("type") != "commit" or sha != commit_sha:
                    raise OperatorError("Created ref differs from its intended commit", ambiguous_mutation=True)
                return sha

            _ensure_object(api, journal, role + "-ref", "/git/refs", {"ref": "refs/heads/" + ref, "sha": commit_sha}, lambda: api.get("/git/ref/heads/" + quote(ref, safe=""), missing_ok=True), validate_ref)
            _remote_ref(api, ref, contents, pinned=commit_sha)
        result = {"ref": ref, "tree_sha": tree_sha, "commit_sha": commit_sha}
        if journal.last("published", role) != result:
            journal.record("published", role, result)
        published[role] = result
        if role == "input":
            _register(root, prepared, commit_sha)
    return published


def _published(journal: Journal) -> dict:
    result = {role: journal.last("published", role) for role in ("input", "oracle")}
    if not all(result.values()):
        raise OperatorError("Publish both immutable refs before capture")
    return result


def _run_fields(run: dict) -> dict:
    return {key: run.get(key) for key in ("id", "run_attempt", "head_sha", "head_branch", "event", "status", "conclusion", "path")}


def _find_push(api, prepared: dict, base_sha: str, deadline) -> dict | None:
    path = "/actions/workflows/" + quote(prepared["hosted_fields"]["workflow"], safe="") + "/runs"
    query = "?event=push&branch=" + quote(prepared["refs"]["input"], safe="") + "&head_sha=" + base_sha + "&per_page=100"
    matches = {}
    for page in range(1, 11):
        document = api.get(path + query + f"&page={page}", deadline=deadline)
        batch = document.get("workflow_runs")
        if not isinstance(batch, list) or any(not isinstance(run, dict) for run in batch):
            raise OperatorError("GitHub returned an invalid workflow-run listing")
        for run in batch:
            if run.get("head_sha") == base_sha and run.get("head_branch") == prepared["refs"]["input"] and run.get("event") == "push":
                if type(run.get("id")) is not int or run["id"] <= 0:
                    raise OperatorError("GitHub returned an invalid push-run identity")
                if str(run.get("path", "")).split("@", 1)[0] != ".github/workflows/" + prepared["hosted_fields"]["workflow"]:
                    raise OperatorError("The matching push used a different workflow")
                matches[run["id"]] = run
        if len(batch) < 100:
            break
    else:
        raise OperatorError("Push-run listing exceeded the bounded 1,000-run search")
    if len(matches) > 1:
        raise OperatorError("Multiple push runs match the immutable input; no outcome was selected", run_ids=sorted(matches))
    return next(iter(matches.values()), None)


def _original_logs(store: EvidenceStore, capture: dict) -> bool:
    archives = [entry for entry in capture["entries"] if entry["kind"] == "archive" and entry["path"] == "raw/logs.zip"]
    if len(archives) != 1 or not any(entry["kind"] == "log" and entry["byte_count"] > 0 for entry in capture["entries"]):
        return False
    _, content = store.read_entry(capture["id"], archives[0]["id"])
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return any(not member.is_dir() and member.file_size > 0 for member in archive.infolist())
    except zipfile.BadZipFile:
        return False


def _capture_status(store: EvidenceStore, capture: dict, prepared: dict, published: dict) -> str:
    run, reference = capture["run"], capture["reference"]
    if not (
        reference["owner"] + "/" + reference["repository"] == prepared["repository"]
        and reference["attempt"] == 1 and run.get("run_attempt") == 1 and run.get("id") == reference["run_id"]
        and capture["source_ref"] == published["input"]["commit_sha"] == run.get("head_sha")
        and run.get("head_branch") == prepared["refs"]["input"] and run.get("event") == "push"
        and run.get("status") == "completed"
        and str(run.get("path", "")).split("@", 1)[0] == ".github/workflows/" + prepared["hosted_fields"]["workflow"]
    ):
        return "identity-mismatch"
    if run.get("conclusion") == "success":
        return "unexpected-success"
    if run.get("conclusion") != "failure":
        return "infrastructure-failure"
    if not _original_logs(store, capture):
        return "missing-original-logs"
    jobs = capture["jobs"]
    checks = [job for job in jobs if job.get("name") == "checks"]
    if len(checks) != 1 or checks[0].get("conclusion") != "failure":
        return "infrastructure-failure"
    steps = checks[0].get("steps", [])
    if any(len([step for step in steps if step.get("name") == name and step.get("conclusion") == "success"]) != 1 for name in _BOOTSTRAP_STEPS):
        return "infrastructure-failure"
    failing = {step.get("name") for step in steps if step.get("conclusion") not in {"success", "skipped", "neutral"}}
    if not failing.intersection(_PROJECT_STEPS) or failing - _PROJECT_STEPS - {"Verify protected evidence"}:
        return "infrastructure-failure"
    if any(job is not checks[0] and job.get("conclusion") not in {"success", "skipped", "neutral"} for job in jobs):
        return "infrastructure-failure"
    return "captured"


def _category(case) -> str:
    return "dependency" if "dependency" in case.tags else ("import" if "import-origin" in case.tags else ("configuration" if "configuration" in case.tags else "test"))


def _make_task(case, prepared: dict, published: dict, capture: dict) -> TaskSpec:
    reference = RunReference(**capture["reference"])
    return TaskSpec.model_validate({
        **prepared["hosted_fields"], "id": case.id, "family": case.family, "split": normalize_split(case.split),
        "category": _category(case), "repository": prepared["repository"], "base_sha": published["input"]["commit_sha"],
        "oracle_ref": published["oracle"]["ref"], "oracle_sha": published["oracle"]["commit_sha"],
        "initial_run_url": reference.html_url, "capture_id": capture["id"], "capture_sha256": fingerprint(capture),
    })


def _task_path(root: Path, case_id: str) -> Path:
    return root / "tasks/by-id" / (case_id + ".json")


def load_task(root: Path | str, case, prepared: dict | None = None, *, repository=DEFAULT_REPOSITORY) -> TaskSpec:
    root = Path(root).resolve()
    prepared = prepared or load_prepared(root, case, repository)
    task = TaskSpec.model_validate(_read_json(_task_path(root, case.id)))
    store = EvidenceStore(root / "evidence")
    capture = store.load(task.capture_id)
    store.verify(task.capture_id)
    published = _published(Journal(root / "cases" / case.id / "journal"))
    if _capture_status(store, capture, prepared, published) != "captured" or fingerprint(task) != fingerprint(_make_task(case, prepared, published, capture)):
        raise OperatorError("Captured task/evidence no longer matches the strict frozen deployment")
    return task


def capture_case(root: Path | str, case, api, *, wait_seconds=900.0, poll_seconds=10.0, clock=time.monotonic, sleep=time.sleep) -> dict:
    if not math.isfinite(wait_seconds) or wait_seconds < 0 or not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise OperatorError("Capture wait must be nonnegative and polling interval positive")
    root = Path(root).resolve()
    prepared = load_prepared(root, case, api.repository)
    journal = Journal(root / "cases" / case.id / "journal")
    published = _published(journal)
    store = EvidenceStore(root / "evidence")
    if _task_path(root, case.id).exists():
        task = load_task(root, case, prepared)
        _register(root, prepared, task.base_sha)
        return {"case_id": case.id, "status": "captured", "run_url": task.initial_run_url, "capture_id": task.capture_id}
    for role, name in (("input", "public"), ("oracle", "trusted")):
        _remote_ref(api, published[role]["ref"], _read_tree(root / "cases" / case.id / name), pinned=published[role]["commit_sha"])
    archived = journal.last("capture-archived")
    if archived:
        capture = store.load(archived["capture_id"])
        store.verify(capture["id"])
        if fingerprint(capture) != archived["capture_sha256"]:
            raise OperatorError("Archived capture checkpoint differs")
        status = _capture_status(store, capture, prepared, published)
        if status != "missing-original-logs":
            return _finish_capture(root, case, prepared, published, journal, capture, status)
    deadline = clock() + wait_seconds
    selected = journal.last("capture-selected")
    while True:
        request_deadline = deadline if wait_seconds > 0 else None  # --wait-seconds 0 performs one bounded read-only poll.
        if selected is None:
            run = _find_push(api, prepared, published["input"]["commit_sha"], request_deadline)
            if run is not None:
                reference = RunReference(*prepared["repository"].split("/"), run["id"], 1)
                selected = {"run_id": run["id"], "attempt": 1, "run_url": reference.html_url}
                journal.record("capture-selected", "push", selected)
        if selected is not None:
            reference = RunReference.parse(selected["run_url"])
            run = api.get(f"/actions/runs/{reference.run_id}/attempts/{reference.attempt}", missing_ok=True, deadline=request_deadline)
            if run is not None:
                observation = _run_fields(run)
                if journal.last("run-observed") != observation:
                    journal.record("run-observed", "push", observation)
                if run.get("status") == "completed":
                    try:
                        with GitHubActionsClient(token=api.token, client=api.client, retry_policy=api.retry_policy, deadline=request_deadline) as reader:
                            collected = reader.collect(reference)
                            artifacts = [artifact for artifact in collected.artifacts if artifact.get("name") == f"bench-ci-{reference.run_id}-{reference.attempt}" and not artifact.get("expired")]
                            if len(artifacts) == 1 and type(artifacts[0].get("id")) is int:
                                try:
                                    content = reader._download_logs(f"/repos/{api.repository}/actions/artifacts/{artifacts[0]['id']}/zip")
                                    collected.attachments.append(SourceFile("public-evidence.zip", content))
                                except GitHubError:
                                    collected.warnings.append("Original public artifact download failed; original attempt logs are retained independently.")
                            capture = store.save(collected)
                    except (GitHubError, EvidenceError, httpx.RequestError, OSError) as exc:
                        journal.record("capture-error", "push", _error_summary(exc))
                        raise OperatorError("Could not archive the exact push attempt; no task capture was manufactured", error_type=type(exc).__name__) from None
                    journal.record("capture-archived", "push", {"capture_id": capture["id"], "capture_sha256": fingerprint(capture), "run_url": reference.html_url})
                    return _finish_capture(root, case, prepared, published, journal, capture, _capture_status(store, capture, prepared, published))
        remaining = deadline - clock()
        if remaining <= 0:
            return {"case_id": case.id, "status": "pending", "run_url": selected["run_url"] if selected else None}
        sleep(min(poll_seconds, remaining))


def _finish_capture(root, case, prepared, published, journal, capture, status):
    report = {"case_id": case.id, "status": status, "run_url": capture["run_url"], "capture_id": capture["id"]}
    if status == "captured":
        task = _make_task(case, prepared, published, capture)
        _write_once(_task_path(root, case.id), task.model_dump(mode="json"))
        _register(root, prepared, task.base_sha)
        if journal.last("task-captured") != report:
            journal.record("task-captured", "push", report)
    elif journal.last("capture-rejected") != report:
        journal.record("capture-rejected", "push", report)
    return report


def export_tasks(root: Path | str, cases, repository: str) -> Path | None:
    root = Path(root).resolve()
    tasks = [load_task(root, case, repository=repository).model_dump(mode="json") for case in cases if _task_path(root, case.id).exists()]
    if not tasks:
        return None
    split = normalize_split(cases[0].split)
    path = root / "tasks" / (split + "-" + _digest(_canonical(tasks))[:24] + ".json")
    _write_once(path, tasks)
    return path


def build_spec(root: Path | str, cases, repository: str, *, identifier: str, output: Path | str, corpus: Path | str | None = None) -> ExperimentSpec:
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise OperatorError("Specification --output must be a new path")
    if output.is_relative_to(root / "cases"):
        raise OperatorError("Specification metadata must remain outside case/deployment trees")
    if not cases or any(not _task_path(root, case.id).is_file() for case in cases):
        raise OperatorError("Every selected case needs a genuine captured task before spec generation")
    tasks = [load_task(root, case, repository=repository) for case in cases]
    memory, memory_fields, identities = None, {}, _identities(root)
    if corpus is not None:
        from buildtrace.experiments.memory import FrozenMemory

        memory = FrozenMemory(Path(corpus).resolve())
        memory_fields = {"corpus": memory.directory, "corpus_sha256": memory.sha256, "insights_sha256": memory.insights_sha256}
        identities.extend({**{key: source[key] for key in ("id", "family", "repository", "base_sha")}, "split": "train"} for source in memory.manifest["training_tasks"])
    registry = create_split_registry(tasks, identities)
    spec = ExperimentSpec(
        schema_version=2, id=identifier, data_kind="live", tasks=tasks,
        model=ModelSettings(model="gpt-6-luna", reasoning_effort="medium", temperature=None),
        budget=Budget(model_calls=60, patch_attempts=5, max_output_tokens=16384, total_tokens=4_000_000, wall_seconds=3600),
        request_timeout_seconds=120, trials=3, seed=42,
        retrieval=RetrievalSettings(query="initial-failed-step-diagnostics-v2", tokenizer="diagnostic-identifiers-v2"),
        execution_parallelism=3,
        split_registry=[TaskIdentity.model_validate(row) for row in registry["identities"]], **memory_fields,
    )
    if memory is not None:
        memory.assert_test_separation(spec)
    _write_once(root / "registries" / (registry["sha256"] + ".json"), registry)
    _write_once(output, spec.model_dump(mode="json"), identical_ok=False)
    return spec


def _error_summary(exc: Exception) -> dict:
    if isinstance(exc, OperatorError):
        return exc.as_dict()
    return {"message": "Operator operation failed", "error_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None), "request_attempts": getattr(exc, "retry_attempts", 1), "errno": getattr(exc, "errno", None)}


def _redact(value, token: str):
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]") if token else value
    if isinstance(value, dict):
        return {key: _redact(item, token) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, token) for item in value]
    return value


def reference_changes(root: Path, case, task: TaskSpec) -> list[dict]:
    """Complete replacement contents, applying inverses to the frozen public tree."""
    files = _read_tree(root / "cases" / case.id / "public")
    original = dict(files)
    changed = set()
    for edit in reference_edits(case.id):
        protected = any(edit.path.startswith(pattern) if pattern.endswith("/") else fnmatch.fnmatchcase(edit.path, pattern) for pattern in task.protected_paths)
        if not is_editable(edit.path) or protected or not any(fnmatch.fnmatchcase(edit.path, pattern) for pattern in task.editable_paths):
            raise OperatorError("A reference inverse touches a noneditable file")
        edit.apply(files)
        changed.add(edit.path)
    result = [{"path": name, "content": files[name].decode("utf-8")} for name in sorted(changed) if files[name] != original[name]]
    if not 1 <= len(result) <= 6:
        raise OperatorError("Reference smoke must change between one and six editable files")
    return result


def smoke_case(root: Path | str, case, api, *, allow_publish=False, timeout_seconds=3600.0, new_smoke=False, backend_factory=None) -> dict:
    if not allow_publish:
        raise OperatorError("Reference smoke requires --allow-publish")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise OperatorError("Smoke timeout must be positive")
    root = Path(root).resolve()
    prepared = load_prepared(root, case, api.repository)
    task = load_task(root, case, prepared)
    changes = reference_changes(root, case, task)
    journal = Journal(root / "cases" / case.id / "journal")
    prior = journal.last("smoke-started")
    if prior and not new_smoke:
        home = root / "smokes" / prior["namespace"]
        intent = _read_sealed(home / "intent.json", "operator-reference-smoke")
        if intent.get("case_id") != case.id or intent.get("namespace") != prior["namespace"] or intent["task_sha256"] != fingerprint(task) or intent["changes"] != changes:
            raise OperatorError("Previous smoke belongs to different frozen inputs")
        if not (home / "result.json").exists():
            raise OperatorError("Previous smoke is incomplete and will not be replayed; inspect its journal or request --new-smoke")
        result = _read_sealed(home / "result.json", "operator-reference-smoke-result")
        if result.get("namespace") != prior["namespace"] or result.get("case_id") != case.id or result.get("intent_sha256") != intent["sha256"]:
            raise OperatorError("Previous smoke result differs from its immutable intent")
        return result
    namespace = "operator-smoke-" + uuid.uuid4().hex[:24]
    home = root / "smokes" / namespace
    intent = _seal(
        "operator-reference-smoke", namespace=namespace, case_id=case.id, task_sha256=fingerprint(task),
        changes=changes, model_trial=False, admit_to_memory=False,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    _write_once(home / "intent.json", intent, identical_ok=False)
    journal.record("smoke-started", namespace, {"namespace": namespace})
    smoke_journal = Journal(home / "journal")
    previous_hook = api.client.on_mutation
    api.client.on_mutation = lambda event, data: smoke_journal.record(event, namespace, data)
    backend, verification, hunks, observation, error = None, None, [], {}, None
    try:
        factory = backend_factory or GitHubTrialBackend
        backend = factory(task, EvidenceStore(root / "evidence"), token=api.token, client=api.client, allow_publish=True, namespace=namespace, retry_policy=api.retry_policy)
        pinned = backend.pin_oracle()
        _write_once(home / "pinned-task.json", pinned.model_dump(mode="json"))
        if fingerprint(pinned) != fingerprint(task):
            raise OperatorError("Oracle pinning changed the captured frozen task")
        result = backend.submit_patch(changes, timeout_seconds=timeout_seconds)
        verification = result.verification
        hunks, observation = result.hunks, result.observation
    except BackendError as exc:
        # Preserve the backend's structured partial verification, captures,
        # timings, hunks and observations, rather than replacing them with str().
        error = exc.as_dict()
        verification, hunks, observation = exc.verification, exc.hunks, exc.observation
    except (OperatorError, GitHubError, EvidenceError, httpx.RequestError, OSError, ValueError) as exc:
        error = _error_summary(exc)
    finally:
        api.client.on_mutation = previous_hook
        if backend is not None:
            try:
                backend.close()
            except (OSError, httpx.RequestError) as exc:
                cleanup_error = _error_summary(exc)
                error = {"operation_error": error, "cleanup_error": cleanup_error} if error else cleanup_error
    report = _seal(
        "operator-reference-smoke-result", namespace=namespace, case_id=case.id, intent_sha256=intent["sha256"],
        model_trial=False, admit_to_memory=False, passed=error is None and verification is not None and verification.passed,
        verification=verification.model_dump(mode="json") if verification else None,
        hunks=[hunk.model_dump(mode="json") for hunk in hunks], observation=observation, error=error,
        capture_ids=sorted(getattr(backend, "allowed_captures", {task.capture_id})),
        ci_operation_seconds=getattr(backend, "ci_operation_seconds", 0),
    )
    # Known authentication material is never persisted, even if a custom backend
    # accidentally includes it in a diagnostic. Reseal after redaction.
    redacted = _redact({key: value for key, value in report.items() if key not in {"sha256", "kind", "schema_version"}}, api.token)
    report = _seal("operator-reference-smoke-result", **redacted)
    _write_once(home / "result.json", report, identical_ok=False)
    journal.record("smoke-completed", namespace, {"namespace": namespace, "passed": report["passed"], "result_sha256": report["sha256"]})
    return report


def _load_environment():
    # Called only by explicitly invoked network commands, never at import,
    # during preparation/specification, or by the offline test fixtures.
    from dotenv import load_dotenv

    load_dotenv(override=False)


def _common_options(parser, *, defaults=True):
    empty = None if defaults else argparse.SUPPRESS
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT if defaults else argparse.SUPPRESS)
    parser.add_argument("--split", type=lambda value: "dev" if value == "development" else value, choices=("train", "dev", "test"), default=empty)
    parser.add_argument("--case", dest="case_ids", action="append", default=[] if defaults else argparse.SUPPRESS)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY if defaults else argparse.SUPPRESS)
    parser.add_argument("--allow-publish", action="store_true", default=False if defaults else argparse.SUPPRESS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _common_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "publish", "capture", "spec", "smoke"):
        command = commands.add_parser(name)
        _common_options(command, defaults=False)
        if name == "capture":
            command.add_argument("--wait-seconds", type=float, default=900)
            command.add_argument("--poll-seconds", type=float, default=10)
        if name == "spec":
            command.add_argument("--id", required=True)
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--corpus", type=Path)
        if name == "smoke":
            command.add_argument("--timeout-seconds", type=float, default=3600)
            command.add_argument("--new-smoke", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.split is None:
            raise OperatorError("--split is required for every operator command")
        if args.command in {"publish", "smoke"} and not args.allow_publish:
            raise OperatorError("This command requires --allow-publish")
        cases = select_cases(args.split, args.case_ids)
        repository = _repository(args.repository)
        root = args.root.resolve()
        if args.command == "prepare":
            for case in cases:
                prepared = prepare_case(root, case, repository)
                print(json.dumps({"case_id": case.id, "status": "prepared", "refs": prepared["refs"]}))
            print(json.dumps({"prepared": len(cases), "split": args.split}))
            return 0
        if args.command == "spec":
            spec = build_spec(root, cases, repository, identifier=args.id, output=args.output, corpus=args.corpus)
            print(json.dumps({"tasks": len(spec.tasks), "registered": len(spec.split_registry), "output": str(args.output), "experiment_sha256": fingerprint(spec)}))
            return 0
        _load_environment()
        reports = []
        with GitHubOperatorAPI(repository) as api:
            for case in cases:
                if args.command == "publish":
                    published = publish_case(root, case, api, allow_publish=args.allow_publish)
                    report = {"case_id": case.id, "status": "published", "refs": published}
                elif args.command == "capture":
                    report = capture_case(root, case, api, wait_seconds=args.wait_seconds, poll_seconds=args.poll_seconds)
                else:
                    result = smoke_case(root, case, api, allow_publish=args.allow_publish, timeout_seconds=args.timeout_seconds, new_smoke=args.new_smoke)
                    verification = result["verification"] or {}
                    report = {"case_id": case.id, "status": "passed" if result["passed"] else "nonpassing", "namespace": result["namespace"], "run_url": verification.get("run_url"), "oracle_run_url": verification.get("oracle_run_url")}
                reports.append(report)
                print(json.dumps(report))
        if args.command == "capture":
            exported = export_tasks(root, cases, repository)
            print(json.dumps({"captured": sum(row["status"] == "captured" for row in reports), "selected": len(cases), "tasks_json": str(exported) if exported else None}))
            return 0 if all(row["status"] == "captured" for row in reports) else 1
        print(json.dumps({"completed": len(reports), "split": args.split}))
        return 0 if all(row["status"] in {"published", "passed"} for row in reports) else 1
    except (OperatorError, EvidenceError, GitHubError, BackendError, httpx.RequestError, OSError, ValueError, KeyError, TypeError) as exc:
        # Do not expose a validation error's input dump or a transport's body/URL.
        print(json.dumps({"error": _error_summary(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
