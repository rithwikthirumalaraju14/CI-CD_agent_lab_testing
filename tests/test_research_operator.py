import base64
import copy
from datetime import datetime
import hashlib
import io
import json
from types import SimpleNamespace
from urllib.parse import unquote
import zipfile

import httpx
import pytest

from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.backend import BackendError, PatchResult, changed_hunks
from buildtrace.experiments.models import ExperimentSpec, TaskSpec, Verification, fingerprint
from buildtrace.models import CollectedRun, RunReference
from buildtrace.research_benchmark.catalogue import get_case
from buildtrace.research_benchmark.materialize import snapshot_files
from buildtrace.research_benchmark import operate as op


REPO = op.DEFAULT_REPOSITORY
TOKEN = "mock-operator-token-not-a-credential"


@pytest.fixture(autouse=True)
def no_live_transport_or_dotenv(monkeypatch):
    import dotenv

    def forbidden(*args, **kwargs):
        pytest.fail("An operator test attempted a real HTTP request or environment-file load")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    monkeypatch.setattr(op, "_load_environment", lambda: None)
    monkeypatch.setattr(op, "resolve_github_token", lambda: TOKEN)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_transient_cleanup_lock_reconciles_identical_already_prepared_trees(tmp_path, monkeypatch):
    original = op._prepare_case_once
    snapshots, waits = [], []
    real_sleep = op.time.sleep

    def wait(delay):
        waits.append(delay)
        real_sleep(delay)

    def interrupted(*args, **kwargs):
        prepared = original(*args, **kwargs)
        snapshots.append(prepared)
        if len(snapshots) == 1:
            raise PermissionError("Temporary scanner lock during cleanup")
        return prepared

    monkeypatch.setattr(op, "_prepare_case_once", interrupted)
    monkeypatch.setattr(op, "time", SimpleNamespace(sleep=wait))
    prepared = op.prepare_case(tmp_path / "operation", get_case("rv2-001"))
    assert snapshots == [prepared, prepared]
    assert waits and len(waits) <= 4 and waits == [0.05, 0.1, 0.2, 0.4][:len(waits)]
    assert op.load_prepared(tmp_path / "operation", get_case("rv2-001"), REPO) == prepared


def _object(kind, data):
    return hashlib.sha1(f"{kind} {len(data)}\0".encode() + data).hexdigest()


def _logs():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(zipfile.ZipInfo("checks/Run protected tests.txt", (2024, 1, 1, 0, 0, 0)), "FAILED test_current_contract\n1 failed, 20 passed\n")
    return stream.getvalue()


class GitHub:
    """In-memory Git/Actions server; every request uses httpx.MockTransport."""

    def __init__(self, clock):
        self.clock = clock
        self.blobs, self.trees, self.commits, self.refs, self.runs = {}, {}, {}, {}, {}
        self.posts, self.requests = [], []
        self.fault = None
        self.transient_gets = 0
        self.missing_logs = False
        self.extra_run = False

    def _commit(self, payload):
        def person(value):
            moment = datetime.fromisoformat(value["date"].replace("Z", "+00:00"))
            return f"{value['name']} <{value['email']}> {int(moment.timestamp())} +0000"

        assert payload["parents"] == []
        content = f"tree {payload['tree']}\nauthor {person(payload['author'])}\ncommitter {person(payload['committer'])}\n\n{payload['message']}".encode()
        sha = _object("commit", content)
        self.commits[sha] = {"sha": sha, "tree": {"sha": payload["tree"]}, "parents": [], "message": payload["message"]}
        return self.commits[sha]

    def add_run(self, ref, sha):
        identifier = 1000 + len(self.runs)
        run = {"id": identifier, "run_attempt": 1, "head_sha": sha, "head_branch": ref, "event": "push", "status": "completed", "conclusion": "failure", "path": ".github/workflows/ci.yml", "html_url": f"https://github.com/{REPO}/actions/runs/{identifier}"}
        self.runs[identifier] = {"run": run, "latest": dict(run), "jobs": self.jobs()}

    @staticmethod
    def jobs(*, bootstrap_failure=None):
        steps = [{"name": name, "conclusion": "failure" if name == bootstrap_failure else "success"} for name in sorted(op._BOOTSTRAP_STEPS)]
        steps.extend({"name": name, "conclusion": ("failure" if name == "Run protected tests" else "skipped") if not bootstrap_failure else "skipped"} for name in sorted(op._PROJECT_STEPS))
        steps += [{"name": "Verify protected evidence", "conclusion": "failure"}, {"name": "Upload execution evidence", "conclusion": "success"}]
        return [{"name": "checks", "status": "completed", "conclusion": "failure", "steps": steps}]

    def __call__(self, request):
        path = unquote(request.url.path.removeprefix("/repos/" + REPO))
        self.requests.append((request.method, path, dict(request.url.params)))
        payload = json.loads(request.content) if request.content else None
        if request.method == "POST":
            self.posts.append((path, copy.deepcopy(payload), self.clock()))
            if self.fault == "ref-before" and path == "/git/refs":
                self.fault = None
                raise httpx.ReadTimeout("private-body " + TOKEN, request=request)
            if path == "/git/blobs":
                assert payload["encoding"] == "base64"
                content = base64.b64decode(payload["content"], validate=True)
                sha = _object("blob", content)
                self.blobs[sha] = content
                response = {"sha": sha}
            elif path == "/git/trees":
                assert set(payload) == {"tree"}  # No inherited default-branch tree.
                files = {item["path"]: self.blobs[item["sha"]] for item in payload["tree"]}
                assert all(item["mode"] == "100644" and item["type"] == "blob" for item in payload["tree"])
                sha = op.git_tree_sha(files)
                self.trees[sha] = {"sha": sha, "truncated": False, "tree": copy.deepcopy(payload["tree"])}
                response = self.trees[sha]
            elif path == "/git/commits":
                response = self._commit(payload)
            elif path == "/git/refs":
                ref = payload["ref"].removeprefix("refs/heads/")
                if ref in self.refs:
                    return httpx.Response(422, json={"message": "private-body " + TOKEN})
                self.refs[ref] = payload["sha"]
                if ref.startswith("buildtrace-inputs/"):
                    self.add_run(ref, payload["sha"])
                response = {"ref": payload["ref"], "object": {"type": "commit", "sha": payload["sha"]}}
            elif path.endswith("/dispatches"):
                return httpx.Response(204)
            else:
                pytest.fail("Unexpected mutation: " + path)
            if self.fault == "commit-after" and path == "/git/commits":
                self.fault = None
                raise httpx.ReadTimeout("private-body " + TOKEN, request=request)
            if self.fault == "input-interrupt" and path == "/git/refs" and payload["ref"].startswith("refs/heads/buildtrace-inputs/"):
                self.fault = None
                raise KeyboardInterrupt()
            return httpx.Response(201, json=response)
        assert request.method == "GET"
        if self.transient_gets:
            self.transient_gets -= 1
            return httpx.Response(503, json={"message": "private-body " + TOKEN})
        if path.startswith("/git/ref/heads/"):
            ref = path.removeprefix("/git/ref/heads/")
            if ref in self.refs:
                return httpx.Response(200, json={"ref": "refs/heads/" + ref, "object": {"type": "commit", "sha": self.refs[ref]}})
        elif path.startswith("/git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            if sha in self.blobs:
                return httpx.Response(200, json={"sha": sha, "encoding": "base64", "content": base64.b64encode(self.blobs[sha]).decode()})
        elif path.startswith("/git/trees/"):
            sha = path.rsplit("/", 1)[1]
            if sha in self.trees:
                return httpx.Response(200, json=self.trees[sha])
        elif path.startswith("/git/commits/"):
            sha = path.rsplit("/", 1)[1]
            if sha in self.commits:
                return httpx.Response(200, json=self.commits[sha])
        elif path == "/actions/workflows/ci.yml/runs":
            runs = [row["latest"] for row in self.runs.values() if row["latest"]["head_sha"] == request.url.params["head_sha"] and row["latest"]["head_branch"] == request.url.params["branch"]]
            if self.extra_run and runs:
                runs.append({**runs[0], "id": runs[0]["id"] + 10000})
            return httpx.Response(200, json={"workflow_runs": runs})
        elif path.startswith("/actions/runs/"):
            parts = path.split("/")
            row = self.runs.get(int(parts[3]))
            if row:
                if len(parts) == 4:
                    return httpx.Response(200, json=row["latest"])
                if parts[4:] == ["artifacts"]:
                    return httpx.Response(200, json={"artifacts": []})
                if parts[4:] == ["attempts", "1"]:
                    return httpx.Response(200, json=row["run"])
                if parts[4:] == ["attempts", "1", "jobs"]:
                    return httpx.Response(200, json={"jobs": row["jobs"]})
                if parts[4:] == ["attempts", "1", "logs"]:
                    return httpx.Response(410 if self.missing_logs else 200, content=b"private-body" if self.missing_logs else _logs())
        elif path.startswith("/contents/"):
            commit = self.commits.get(request.url.params["ref"])
            if commit:
                name = path.removeprefix("/contents/")
                entry = next((item for item in self.trees[commit["tree"]["sha"]]["tree"] if item["path"] == name), None)
                if entry:
                    content = self.blobs[entry["sha"]]
                    return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": len(content), "content": base64.b64encode(content).decode()})
        return httpx.Response(404, json={"message": "private-body " + TOKEN})


@pytest.fixture
def remote():
    clock = Clock()
    server = GitHub(clock)
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        api = op.GitHubOperatorAPI(REPO, token=TOKEN, client=client, retry_policy=op.GetRetryPolicy(base_delay_seconds=0, max_delay_seconds=0, max_wait_seconds=0), clock=clock, sleep=clock.sleep)
        yield api, server, clock


def _prepare(root, case_id="rv2-001"):
    case = get_case(case_id)
    return case, op.prepare_case(root, case, REPO)


def _captured(root, remote, case_id="rv2-001"):
    api, server, clock = remote
    case, prepared = _prepare(root, case_id)
    published = op.publish_case(root, case, api, allow_publish=True)
    report = op.capture_case(root, case, api, wait_seconds=0)
    assert report["status"] == "captured", report
    return case, prepared, published, op.load_task(root, case, prepared)


def test_git_tree_identity_uses_git_directory_sorting_and_binary_bytes():
    files = {"a/z": b"\x00\xff", "a.txt": b"text\n", "z": b""}
    subtree = _object("tree", b"100644 z\0" + bytes.fromhex(_object("blob", b"\x00\xff")))
    frames = b"100644 a.txt\0" + bytes.fromhex(_object("blob", b"text\n")) + b"40000 a\0" + bytes.fromhex(subtree) + b"100644 z\0" + bytes.fromhex(_object("blob", b""))
    assert op.git_tree_sha(files) == _object("tree", frames)
    assert op.git_object_sha("blob", b"test content\n") == "d670460b4b4aece5915caf5c68d12f560a9fe3e4"


def test_prepare_separates_all_operator_data_and_stable_opaque_refs(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    case, prepared = _prepare(first, "rv2-015")
    again = op.prepare_case(first, case, REPO)
    other = op.prepare_case(second, case, REPO)
    assert prepared == again
    assert prepared["refs"] == other["refs"]
    public = op._read_tree(first / "cases" / case.id / "public")
    trusted = op._read_tree(first / "cases" / case.id / "trusted")
    assert set(trusted) == set(op._SHARED) | {op._CONTRACT, "infra/research_v2/oracle/tests/test_oracle.py", "infra/research_v2/oracle/fixtures/checks.json"}
    assert set(json.loads(public[op._SHARED[2]])) == {"schema_version", "expected_public_tests"}
    assert all(public[name] == trusted[name] for name in op._SHARED)
    for name, content in public.items():
        assert "oracle/" not in name and "manifest.json" not in name and "prepared.json" not in name
        assert b"rv2-015" not in content and str(tmp_path).encode() not in content and b"reference_edits" not in content
    assert prepared["hosted_fields"]["expected_test_count"] == 20
    assert prepared["hosted_fields"]["expected_oracle_test_count"] == 14
    for ref in prepared["refs"].values():
        assert len(ref.rsplit("rv2-", 1)[1]) == 24
        assert "train" not in ref and case.family not in ref and case.id not in ref
    manifest = json.loads((first / "cases" / case.id / "bundle/manifest.json").read_bytes())
    assert manifest["destination"] == str(first / "cases" / case.id / "raw")


def test_prepare_resumes_partial_identical_trees_but_never_overwrites(tmp_path):
    case, prepared = _prepare(tmp_path)
    marker = tmp_path / "cases" / case.id / "prepared.json"
    marker.unlink()  # Simulated interruption after moving the last tree.
    assert op.prepare_case(tmp_path, case, REPO) == prepared
    app = tmp_path / "cases" / case.id / "public/src/bench_app/app.py"
    app.write_bytes(b"operator work that must survive\n")
    with pytest.raises(op.OperatorError, match="differs"):
        op.prepare_case(tmp_path, case, REPO)
    assert app.read_bytes() == b"operator work that must survive\n"
    with pytest.raises(op.OperatorError, match="hash differs"):
        op.load_prepared(tmp_path, case, REPO)


def test_publish_exact_binary_blobs_orphans_and_idempotent_refs(tmp_path, remote):
    api, server, clock = remote
    case, prepared = _prepare(tmp_path, "rv2-015")
    publication = op.publish_case(tmp_path, case, api, allow_publish=True)
    writes = len(server.posts)
    assert op.publish_case(tmp_path, case, api, allow_publish=True) == publication
    assert len(server.posts) == writes
    for role, name in (("input", "public"), ("oracle", "trusted")):
        contents = op._read_tree(tmp_path / "cases" / case.id / name)
        commit = server.commits[publication[role]["commit_sha"]]
        actual = {entry["path"]: server.blobs[entry["sha"]] for entry in server.trees[commit["tree"]["sha"]]["tree"]}
        assert actual == contents
        assert commit["parents"] == []
        assert case.id not in commit["message"] and case.family not in commit["message"] and "train" not in commit["message"]
    assert all(later[2] - earlier[2] >= 1.0 for earlier, later in zip(server.posts, server.posts[1:]))
    assert all(method in {"GET", "POST"} for method, _, _ in server.requests)
    assert op._identities(tmp_path)[0]["base_sha"] == publication["input"]["commit_sha"]


def test_publication_blob_cache_is_shared_across_cases(tmp_path, remote):
    api, server, clock = remote
    for case_id in ("rv2-001", "rv2-003"):
        case, _ = _prepare(tmp_path, case_id)
        op.publish_case(tmp_path, case, api, allow_publish=True)
    blobs = [payload["content"] for path, payload, _ in server.posts if path == "/git/blobs"]
    assert len(blobs) == len(set(blobs))
    assert len(op._identities(tmp_path)) == 2


def test_matching_existing_orphan_ref_is_adopted_without_moving_it(tmp_path, remote):
    api, server, clock = remote
    case, prepared = _prepare(tmp_path / "a")
    op.publish_case(tmp_path / "a", case, api, allow_publish=True)
    # Another operator root can encounter a matching tree with different commit
    # metadata. The actual existing orphan SHA, not a planned replacement, wins.
    ref = prepared["refs"]["input"]
    tree = server.commits[server.refs[ref]]["tree"]["sha"]
    payload, _ = op._commit_plan(tree, "unrelated-opaque-identity")
    existing = server._commit(payload)["sha"]
    server.refs[ref] = existing
    op.prepare_case(tmp_path / "b", case, REPO)
    before = len(server.posts)
    result = op.publish_case(tmp_path / "b", case, api, allow_publish=True)
    assert result["input"]["commit_sha"] == existing
    assert server.refs[ref] == existing and len(server.posts) == before
    with pytest.raises(op.OperatorError, match="moved"):
        op.publish_case(tmp_path / "a", case, api, allow_publish=True)


def test_mismatched_existing_ref_prevents_all_mutations(tmp_path, remote):
    api, server, clock = remote
    case, prepared = _prepare(tmp_path / "a")
    published = op.publish_case(tmp_path / "a", case, api, allow_publish=True)
    server.refs[prepared["refs"]["input"]] = published["oracle"]["commit_sha"]
    op.prepare_case(tmp_path / "b", case, REPO)
    before = len(server.posts)
    with pytest.raises(op.OperatorError, match="intended orphan"):
        op.publish_case(tmp_path / "b", case, api, allow_publish=True)
    assert len(server.posts) == before


def test_ambiguous_accepted_commit_is_reconciled_without_reposting(tmp_path, remote):
    api, server, clock = remote
    case, _ = _prepare(tmp_path)
    server.fault = "commit-after"
    op.publish_case(tmp_path, case, api, allow_publish=True)
    commits = [payload for path, payload, _ in server.posts if path == "/git/commits"]
    assert len(commits) == 2  # Exactly one per role, including the timed-out POST.
    text = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "cases" / case.id / "journal").glob("*.json"))
    assert TOKEN not in text and "private-body" not in text


def test_unresolved_post_is_never_blindly_retried(tmp_path, remote):
    api, server, clock = remote
    case, _ = _prepare(tmp_path)
    server.fault = "ref-before"
    with pytest.raises(op.OperatorError):
        op.publish_case(tmp_path, case, api, allow_publish=True)
    before = len(server.posts)
    with pytest.raises(op.OperatorError, match="POST was not repeated"):
        op.publish_case(tmp_path, case, api, allow_publish=True)
    assert len(server.posts) == before


def test_interrupted_ref_publication_resumes_from_reads(tmp_path, remote):
    api, server, clock = remote
    case, _ = _prepare(tmp_path)
    server.fault = "input-interrupt"
    with pytest.raises(KeyboardInterrupt):
        op.publish_case(tmp_path, case, api, allow_publish=True)
    # Registration precedes the input-ref POST, so another active split can
    # carry this actual commit identity even before this case is resumed.
    assert op._identities(tmp_path)[0]["base_sha"] in server.refs.values()
    before = len(server.posts)
    result = op.publish_case(tmp_path, case, api, allow_publish=True)
    assert len(server.posts) == before
    assert op._identities(tmp_path)[0]["base_sha"] == result["input"]["commit_sha"]


def test_gets_use_bounded_retries_and_sanitized_failures(tmp_path, remote):
    api, server, clock = remote
    server.transient_gets = 2
    assert api.get("/git/ref/heads/missing", missing_ok=True) is None
    assert len(server.requests) == 3
    server.transient_gets = 8
    with pytest.raises(op.OperatorError) as caught:
        api.get("/git/ref/heads/missing")
    assert caught.value.details["request_attempts"] == 3
    assert TOKEN not in json.dumps(caught.value.as_dict()) and "private-body" not in json.dumps(caught.value.as_dict())


def test_capture_archives_original_attempt_not_a_later_rerun(tmp_path, remote):
    api, server, clock = remote
    case, prepared = _prepare(tmp_path, "rv2-015")
    published = op.publish_case(tmp_path, case, api, allow_publish=True)
    row = next(iter(server.runs.values()))
    row["latest"].update(run_attempt=2, conclusion="success")
    report = op.capture_case(tmp_path, case, api, wait_seconds=0)
    assert report["status"] == "captured" and report["run_url"].endswith("/attempts/1")
    task = op.load_task(tmp_path, case, prepared)
    assert task.schema_version == 2 and task.artifact_prefix == "bench-ci"
    assert (task.expected_test_count, task.expected_oracle_test_count) == (20, 14)
    assert task.oracle_sha == published["oracle"]["commit_sha"]
    assert task.verification_contract_sha256 == prepared["hosted_fields"]["verification_contract_sha256"]
    assert task.editable_environment_keys == []
    store = EvidenceStore(tmp_path / "evidence")
    capture = store.load(task.capture_id)
    assert capture["run"]["run_attempt"] == 1 and capture["run"]["conclusion"] == "failure"
    assert task.capture_sha256 == fingerprint(capture)
    archive = next(entry for entry in capture["entries"] if entry["kind"] == "archive")
    assert store.read_entry(task.capture_id, archive["id"])[1] == _logs()
    before = len(server.requests)
    assert op.capture_case(tmp_path, case, api, wait_seconds=0) == report
    assert len(server.requests) == before
    exported = op.export_tasks(tmp_path, [case], REPO)
    assert json.loads(exported.read_bytes()) == [task.model_dump(mode="json")]
    for field in ("expected_oracle_test_count", "expected_test_count", "protected_paths", "verification_contract_sha256"):
        weakened = task.model_dump(mode="json")
        weakened.pop(field)
        with pytest.raises(ValueError):
            TaskSpec.model_validate(weakened)


@pytest.mark.parametrize("outcome,expected", [("success", "unexpected-success"), ("timed_out", "infrastructure-failure"), ("bootstrap", "infrastructure-failure"), ("identity", "identity-mismatch")])
def test_unexpected_success_and_infrastructure_are_preserved_not_tasks(tmp_path, remote, outcome, expected):
    api, server, clock = remote
    case, prepared = _prepare(tmp_path)
    op.publish_case(tmp_path, case, api, allow_publish=True)
    row = next(iter(server.runs.values()))
    if outcome == "bootstrap":
        row["jobs"] = server.jobs(bootstrap_failure="Provision pinned verification tools")
    elif outcome == "identity":
        row["run"]["head_sha"] = "f" * 40
    else:
        row["run"]["conclusion"] = outcome
    report = op.capture_case(tmp_path, case, api, wait_seconds=0)
    assert report["status"] == expected
    assert not op._task_path(tmp_path, case.id).exists()
    store = EvidenceStore(tmp_path / "evidence")
    assert store.verify(report["capture_id"])["integrity"] == "verified"
    archived = store.load(report["capture_id"])
    assert archived["run"] == row["run"]
    assert op.capture_case(tmp_path, case, api, wait_seconds=0) == report
    assert len(store.recent()) == 1


def test_missing_logs_can_recover_same_pinned_attempt_without_erasing_capture(tmp_path, remote):
    api, server, clock = remote
    case, _ = _prepare(tmp_path)
    op.publish_case(tmp_path, case, api, allow_publish=True)
    server.missing_logs = True
    incomplete = op.capture_case(tmp_path, case, api, wait_seconds=0)
    assert incomplete["status"] == "missing-original-logs"
    assert not op._task_path(tmp_path, case.id).exists()
    server.missing_logs = False
    complete = op.capture_case(tmp_path, case, api, wait_seconds=0)
    assert complete["status"] == "captured"
    assert complete["run_url"] == incomplete["run_url"]
    assert complete["capture_id"] != incomplete["capture_id"]
    assert len(EvidenceStore(tmp_path / "evidence").recent()) == 2


def test_pending_capture_is_foreground_resumable_and_run_ambiguity_rejected(tmp_path, remote):
    api, server, clock = remote
    case, _ = _prepare(tmp_path)
    op.publish_case(tmp_path, case, api, allow_publish=True)
    row = next(iter(server.runs.values()))
    row["run"].update(status="in_progress", conclusion=None)
    pending = op.capture_case(tmp_path, case, api, wait_seconds=0)
    assert pending["status"] == "pending" and pending["run_url"].endswith("/attempts/1")
    row["run"].update(status="completed", conclusion="failure")
    assert op.capture_case(tmp_path, case, api, wait_seconds=0)["status"] == "captured"
    other_case, _ = _prepare(tmp_path, "rv2-003")
    op.publish_case(tmp_path, other_case, api, allow_publish=True)
    server.extra_run = True
    with pytest.raises(op.OperatorError, match="Multiple push runs"):
        op.capture_case(tmp_path, other_case, api, wait_seconds=0)
    assert not op._task_path(tmp_path, other_case.id).exists()


def test_spec_has_explicit_common_controls_and_all_published_split_identities(tmp_path, remote):
    train, _, _, train_task = _captured(tmp_path, remote)
    dev, _, _, dev_task = _captured(tmp_path, remote, "rv2-005")
    # A previously published held-out identity is operator metadata only. No
    # held-out raw/project/trusted tree or task file is needed or opened here.
    held_out = {"id": "rv2-007", "family": get_case("rv2-007").family, "split": "test", "repository": REPO, "base_sha": "c" * 40}
    op._write_once(tmp_path / "identities/rv2-007.json", op._seal("registered-identity", identity=held_out))
    output = tmp_path / "specs/train.json"
    spec = op.build_spec(tmp_path, [train], REPO, identifier="research-train-check", output=output)
    assert spec.schema_version == 2 and [task.id for task in spec.tasks] == [train.id]
    assert spec.budget.model_dump() == {"model_calls": 60, "patch_attempts": 5, "max_output_tokens": 16384, "total_tokens": 4_000_000, "wall_seconds": 3600.0}
    assert spec.model.model_dump() == {"provider": "openai", "model": "gpt-6-luna", "temperature": None, "reasoning_effort": "medium"}
    assert spec.request_timeout_seconds == 120 and spec.trials == 3 and spec.seed == 42
    assert spec.retrieval == op.RetrievalSettings(query="initial-failed-step-diagnostics-v2", tokenizer="diagnostic-identifiers-v2")
    assert spec.execution_parallelism == 3
    assert {row.id for row in spec.split_registry} == {train.id, dev.id, held_out["id"]}
    assert ExperimentSpec.model_validate_json(output.read_bytes()) == spec
    original = output.read_bytes()
    with pytest.raises(op.OperatorError, match="new path"):
        op.build_spec(tmp_path, [dev], REPO, identifier="other", output=output)
    assert output.read_bytes() == original
    assert not (tmp_path / "cases/rv2-007").exists()


def test_spec_carries_verified_frozen_memory_hashes(tmp_path, remote, monkeypatch):
    from buildtrace.experiments import memory

    train, _, _, train_task = _captured(tmp_path, remote)
    dev, _, _, dev_task = _captured(tmp_path, remote, "rv2-005")
    checked = []

    class Frozen:
        def __init__(self, directory):
            self.directory, self.sha256, self.insights_sha256 = directory, "d" * 64, "e" * 64
            self.manifest = {"training_tasks": [{key: train_task.model_dump()[key] for key in ("id", "family", "repository", "base_sha")}]}

        def assert_test_separation(self, spec):
            checked.append(spec)

    monkeypatch.setattr(memory, "FrozenMemory", Frozen)
    spec = op.build_spec(tmp_path, [dev], REPO, identifier="dev-with-corpus", output=tmp_path / "spec.json", corpus=tmp_path / "frozen-corpus")
    assert spec.corpus == tmp_path / "frozen-corpus"
    assert spec.corpus_sha256 == "d" * 64 and spec.insights_sha256 == "e" * 64
    assert checked == [spec]


def test_train_and_dev_commands_never_materialize_or_publish_test_cases(tmp_path, remote, monkeypatch, capsys):
    api, server, clock = remote
    materialized = []
    original = op.materialize_case

    def record(case_id, *args, **kwargs):
        assert get_case(case_id).split != "test"
        materialized.append(case_id)
        return original(case_id, *args, **kwargs)

    monkeypatch.setattr(op, "materialize_case", record)
    monkeypatch.setattr(op, "GitHubOperatorAPI", lambda repository: api)
    assert op.main(["--root", str(tmp_path), "prepare", "--split", "train", "--case", "rv2-001"]) == 0
    assert op.main(["prepare", "--root", str(tmp_path), "--split", "development", "--case", "rv2-005"]) == 0
    assert op.main(["publish", "--root", str(tmp_path), "--split", "train", "--case", "rv2-001", "--allow-publish"]) == 0
    assert op.main(["capture", "--root", str(tmp_path), "--split", "train", "--case", "rv2-001", "--wait-seconds", "0"]) == 0
    assert op.main(["spec", "--root", str(tmp_path), "--split", "train", "--case", "rv2-001", "--id", "one-train", "--output", str(tmp_path / "spec.json")]) == 0
    assert materialized == ["rv2-001", "rv2-005"]
    assert {path.name for path in (tmp_path / "cases").iterdir()} == {"rv2-001", "rv2-005"}
    assert len(server.refs) == 2
    assert TOKEN not in capsys.readouterr().out
    before = len(server.requests)
    assert op.main(["prepare", "--root", str(tmp_path), "--split", "train", "--case", "rv2-007"]) == 2
    assert len(server.requests) == before


class SmokeBackend:
    instances = []
    failure = False
    interrupt = False

    def __init__(self, task, store, **kwargs):
        self.task, self.store, self.options = task, store, kwargs
        self.allowed_captures = {task.capture_id}
        self.ci_operation_seconds = 2.5
        self.closed = False
        self.instances.append(self)

    def pin_oracle(self):
        return self.task

    def submit_patch(self, changes, *, timeout_seconds):
        assert timeout_seconds > 0
        assert self.options["allow_publish"] is True
        assert self.options["namespace"].startswith("operator-smoke-") and self.task.id not in self.options["namespace"]
        healthy = snapshot_files(self.task.id, correct_baseline=True)
        for change in changes:
            assert op.is_editable(change["path"])
            assert change["content"].encode() == healthy[change["path"]]
        assert changes
        if self.interrupt:
            raise KeyboardInterrupt()
        # Exercise the exact client passed to the backend, including its mutation
        # checkpoint hook. The endpoint is the in-memory mock, never GitHub.
        response = self.options["client"].request("POST", op.API_ROOT + "/repos/" + REPO + "/actions/workflows/ci.yml/dispatches", json={"ref": self.options["namespace"]})
        response.close()
        capture = self.store.save(CollectedRun(
            RunReference(*REPO.split("/"), 9001, 1),
            {"id": 9001, "run_attempt": 1, "event": "workflow_dispatch", "head_sha": "e" * 40, "conclusion": "success", "status": "completed"},
            [], "e" * 40, log_archive=_logs(),
        ))
        self.allowed_captures.add(capture["id"])
        verification = Verification(
            attempt=1, candidate_sha="e" * 40, run_url=f"https://github.com/{REPO}/actions/runs/9001/attempts/1",
            oracle_run_url=f"https://github.com/{REPO}/actions/runs/9002/attempts/1", workflow_conclusion="success",
            required_checks_passed=True, protected_files_unchanged=True, oracle_passed=not self.failure,
            elapsed_seconds=2.5, capture_id=capture["id"], details="mocked operator verification",
        )
        originals = {change["path"]: "old\n" for change in changes}
        hunks = changed_hunks(originals, {change["path"]: change["content"] for change in changes})
        observation = {"current_logs": [{"entry_id": capture["id"] + "/entry-0001"}], "kept": "raw structured observation"}
        if self.failure:
            error = BackendError("Mock transport failure", diagnostics={"target_capture_id": capture["id"], "oracle_capture_id": None, "token_echo": TOKEN}, elapsed_seconds=2.5)
            error.verification, error.hunks, error.observation = verification, hunks, observation
            raise error
        return PatchResult(verification, hunks, observation)

    def close(self):
        self.closed = True


@pytest.fixture
def smoke_backend(monkeypatch):
    monkeypatch.setattr(SmokeBackend, "instances", [])
    monkeypatch.setattr(SmokeBackend, "failure", False)
    monkeypatch.setattr(SmokeBackend, "interrupt", False)
    return SmokeBackend


def test_reference_smoke_saves_complete_edits_verification_and_is_not_a_trial(tmp_path, remote, smoke_backend):
    api, server, clock = remote
    case, prepared, published, task = _captured(tmp_path, remote, "rv2-015")
    result = op.smoke_case(tmp_path, case, api, allow_publish=True, backend_factory=smoke_backend)
    assert result["passed"] and result["error"] is None
    assert result["model_trial"] is False and result["admit_to_memory"] is False
    assert result["verification"]["oracle_passed"] and result["hunks"]
    home = tmp_path / "smokes" / result["namespace"]
    intent = op._read_sealed(home / "intent.json", "operator-reference-smoke")
    assert {change["path"] for change in intent["changes"]} == {"src/bench_app/app.py", "ci/constraints.txt"}
    assert result["intent_sha256"] == intent["sha256"]
    journal = op.Journal(home / "journal")
    assert [event["event"] for event in journal.events] == ["http-intent", "http-response"]
    assert smoke_backend.instances[0].closed
    assert not (tmp_path / "trials").exists()
    assert op.smoke_case(tmp_path, case, api, allow_publish=True, backend_factory=smoke_backend) == result
    assert len(smoke_backend.instances) == 1


def test_backend_error_preserves_partial_capture_as_dict_hunks_and_observation(tmp_path, remote, smoke_backend):
    api, server, clock = remote
    case, prepared, published, task = _captured(tmp_path, remote)
    smoke_backend.failure = True
    result = op.smoke_case(tmp_path, case, api, allow_publish=True, backend_factory=smoke_backend)
    assert not result["passed"] and result["verification"] is not None
    assert result["error"]["elapsed_seconds"] == 2.5
    assert result["error"]["verification"] == result["verification"]
    assert result["error"]["hunks"] == result["hunks"]
    assert result["observation"]["kept"] == "raw structured observation"
    assert result["error"]["diagnostics"]["token_echo"] == "[REDACTED]"
    assert TOKEN not in json.dumps(result)
    store = EvidenceStore(tmp_path / "evidence")
    for capture_id in result["capture_ids"]:
        assert store.verify(capture_id)["integrity"] == "verified"
    original = (tmp_path / "smokes" / result["namespace"] / "result.json").read_bytes()
    assert not op.smoke_case(tmp_path, case, api, allow_publish=True, backend_factory=smoke_backend)["passed"]
    assert len(smoke_backend.instances) == 1
    smoke_backend.failure = False
    fresh = op.smoke_case(tmp_path, case, api, allow_publish=True, new_smoke=True, backend_factory=smoke_backend)
    assert fresh["passed"] and fresh["namespace"] != result["namespace"]
    assert (tmp_path / "smokes" / result["namespace"] / "result.json").read_bytes() == original


def test_interrupted_smoke_is_not_automatically_resubmitted(tmp_path, remote, smoke_backend):
    api, server, clock = remote
    case, prepared, published, task = _captured(tmp_path, remote)
    smoke_backend.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        op.smoke_case(tmp_path, case, api, allow_publish=True, backend_factory=smoke_backend)
    assert smoke_backend.instances[0].closed
    with pytest.raises(op.OperatorError, match="incomplete.*not be replayed"):
        op.smoke_case(tmp_path, case, api, allow_publish=True, backend_factory=smoke_backend)
    assert len(smoke_backend.instances) == 1


def test_cli_requires_publish_authorization_and_nonpassing_smoke_is_nonzero(tmp_path, remote, smoke_backend, monkeypatch, capsys):
    api, server, clock = remote
    monkeypatch.setattr(op, "GitHubOperatorAPI", lambda repository: api)
    monkeypatch.setattr(op, "GitHubTrialBackend", smoke_backend)
    for command in ("publish", "smoke"):
        assert op.main([command, "--root", str(tmp_path), "--split", "train", "--case", "rv2-001"]) == 2
    assert not server.requests and not tmp_path.joinpath("cases").exists()
    _captured(tmp_path, remote)
    smoke_backend.failure = True
    assert op.main(["smoke", "--root", str(tmp_path), "--split", "train", "--case", "rv2-001", "--allow-publish"]) == 1
    output = capsys.readouterr().out
    assert TOKEN not in output and "private-body" not in output and "nonpassing" in output
