"""Operator-side research-v2 identities, mechanism gate, and separated reviews.

These utilities deliberately do not replace the v1 schemas, runner, or reports.
Public functions accept validated models or their JSON ``model_dump`` values.
Nothing here loads environment files, contacts a service, or writes an artifact.
"""

import ast
import copy
import fnmatch
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import platform
import random
import re
import secrets
import sys
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import Enum
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit


MODES = ("no-memory", "insights-only", "wikitrace")
SPLITS = ("train", "dev", "test")
SCHEDULE_POLICY = "sorted-task-shuffle-cyclic-mode-order-v2"
GATE_POLICY = {
    "version": 2,
    "repeats": 3,
    "modes": list(MODES),
    "minimum_predeclared_tasks": 2,
    "different_specific_families": True,
    "minimum_qualifying_wikitrace_repeats_per_task": 2,
    "minimum_verified_repair_chains": 1,
    "source": "nonempty-exact-linked-original-capture-or-successful-raw-tool-payload",
    "freshness": "adjacent-plan-history-burst-plan-without-intervening-observations",
    "materiality": "independent-change-or-refinement-with-different-actionable-content",
    "adoption": "subsequent-observable-action-with-the-reviewed-plan-snapshot",
    "superiority_required": False,
}
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
_TRIGGERS = {"missing-detail", "conflict", "failed-source", "environment-difference", "failed-fix", "uncertainty"}
_COARSE_FAMILIES = {
    "ci", "python", "general", "unknown", "other", "dependency", "dependencies",
    "import", "imports", "test", "tests", "configuration", "config", "parsing",
    "encoding", "timezone", "rounding", "metadata", "resource", "data-processing",
}
_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".pyd", ".so", ".dll", ".j2", ".jinja", ".jinja2",
    ".json", ".toml", ".yaml", ".yml", ".txt", ".ini", ".cfg", ".lock",
    ".pem", ".crt", ".typed", ".template", ".md",
}
_NON_OBSERVATIONS = {
    "metadata.md", "manifest.json", "verification.json", "verification-proof.json",
    "test-execution.json",
}


class ResearchControlError(ValueError):
    """An input, replay, or freeze violates the research-v2 contract."""


def _plain(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Enum):
        return _plain(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ResearchControlError("JSON object keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ResearchControlError("Use validated models or JSON-compatible values")


def content_sha256(value) -> str:
    """Canonical UTF-8 JSON identity; filesystem locations are not special-cased."""
    try:
        payload = json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ResearchControlError("Use finite, JSON-compatible values") from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _byte_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _seal(kind: str, **fields) -> dict:
    value = {"schema_version": 2, "kind": kind, **_plain(fields)}
    return {**value, "sha256": content_sha256(value)}


def _check_seal(value, kind: str) -> dict:
    value = _plain(value)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 2 or value.get("kind") != kind:
        raise ResearchControlError(f"Expected a v2 {kind}")
    if value.get("sha256") != content_sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ResearchControlError(f"Content address mismatch: {kind}")
    return value


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchControlError(f"Missing {label}")
    return value


def _digest(value, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ResearchControlError(f"Invalid SHA-256 for {label}")
    return value


def _integer(value, label: str, minimum=0) -> int:
    if type(value) is not int or value < minimum:
        raise ResearchControlError(f"Invalid {label}")
    return value


def _no_env(parts) -> None:
    if any(part.casefold().startswith(".env") or part.casefold().endswith(".env") for part in parts):
        raise ResearchControlError("Environment files are outside the runtime source closure")


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ResearchControlError("Use a canonical relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or value == ".":
        raise ResearchControlError("Use a canonical relative POSIX path")
    _no_env(path.parts)
    return value


def _local_file(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(_relative(relative)).parts).resolve()
    _no_env(path.parts)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ResearchControlError(f"Missing or escaping runtime file: {relative}")
    return path


def _portable(value) -> None:
    """Reject local locations and credential fields instead of hashing their spelling."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in {"api_key", "access_token", "password", "authorization", "credentials", "secret"}:
                raise ResearchControlError("Credentials do not belong in a research manifest")
            _portable(key)
            _portable(item)
    elif isinstance(value, list):
        for item in value:
            _portable(item)
    elif isinstance(value, str):
        if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
            raise ResearchControlError("Use content identities and relative paths, not absolute paths")
        if "://" in value:
            url = urlsplit(value)
            if url.username or url.password or url.query or url.fragment:
                raise ResearchControlError("Use credential-free, pinned URLs in manifests")


def _identity(task) -> dict:
    task = _plain(task)
    if not isinstance(task, dict):
        raise ResearchControlError("Task identities must be objects")
    result = {name: _text(task.get(name), f"task {name}") for name in ("id", "split", "repository", "base_sha", "family")}
    if result["split"] not in SPLITS or not _IDENTIFIER.fullmatch(result["id"]) or not _IDENTIFIER.fullmatch(result["family"]):
        raise ResearchControlError("Invalid task ID, specific family, or split")
    result["repository"] = result["repository"].casefold()
    result["base_sha"] = result["base_sha"].lower()
    result["family"] = result["family"].casefold()
    if not _REPOSITORY.fullmatch(result["repository"]) or not _REVISION.fullmatch(result["base_sha"]):
        raise ResearchControlError("Task identity needs a repository and full base SHA")
    return result


def create_split_registry(tasks: Iterable = (), registry=None) -> dict:
    """Merge a cross-spec registry, rejecting ID/snapshot/family reuse across splits.

    An exact identity, family, or repository/snapshot may recur within its own
    split. An ID cannot be silently rebound to another identity, even there.
    ``registry`` may be a previous sealed registry or the planned spec list.
    """
    if hasattr(registry, "model_dump"):
        registry = _plain(registry)
    if registry is None:
        prior = []
    elif isinstance(registry, Mapping):
        prior = _check_seal(registry, "research-split-registry").get("identities", [])
    else:
        prior = list(registry)
    unique, ids, snapshots, families = {}, {}, {}, {}
    for task in [*prior, *tasks]:
        identity = _identity(task)
        split = identity["split"]
        key = identity["id"].casefold()
        if key in ids and ids[key] != identity:
            raise ResearchControlError(f"Task ID is already registered with another identity/split: {identity['id']}")
        ids[key] = identity
        for index, component, label in (
            (snapshots, (identity["repository"], identity["base_sha"]), "repository/base_sha"),
            (families, identity["family"], "specific family"),
        ):
            if component in index and index[component] != split:
                raise ResearchControlError(f"Cross-split {label} leakage involving {identity['id']}")
            index[component] = split
        unique[content_sha256(identity)] = identity
    ordered = sorted(unique.values(), key=lambda row: (row["split"], row["id"]))
    return _seal("research-split-registry", identities=ordered)


def verify_split_registry(registry) -> str:
    checked = _check_seal(registry, "research-split-registry")
    if create_split_registry(checked.get("identities", [])) != checked:
        raise ResearchControlError("The split registry is not canonical")
    return checked["sha256"]


def _module_file(root: Path, module: str) -> str | None:
    base = "src/" + module.replace(".", "/")
    for relative in (base + ".py", base + "/__init__.py"):
        if root.joinpath(*PurePosixPath(relative).parts).is_file():
            return relative
    return None


def _source_files(root: Path, extra_files) -> tuple[dict, set[str]]:
    directory = root / "src/buildtrace/experiments"
    sources = {path.relative_to(root).as_posix(): path for path in directory.glob("*.py") if path.is_file()}
    for name in ("__init__", "evidence", "github", "models"):
        relative = f"src/buildtrace/{name}.py"
        sources[relative] = _local_file(root, relative)
    sources["pyproject.toml"] = _local_file(root, "pyproject.toml")
    if "src/buildtrace/experiments/research.py" not in sources:
        raise ResearchControlError("The explicit experiments source closure is incomplete")
    extras = extra_files.items() if isinstance(extra_files, Mapping) else ((None, item) for item in extra_files)
    for label, location in extras:
        path = Path(location)
        _no_env(path.parts)
        path = (path if path.is_absolute() else root / path).resolve()
        _no_env(path.parts)
        if label is None:
            if not path.is_relative_to(root):
                raise ResearchControlError("Give an external runtime file an explicit relative logical label")
            label = path.relative_to(root).as_posix()
        label = _relative(label)
        if not path.is_file() or path.suffix.casefold() not in _SOURCE_SUFFIXES:
            raise ResearchControlError(f"Not an explicit runtime source/control file: {label}")
        if label in sources and sources[label].resolve() != path:
            raise ResearchControlError(f"Duplicate runtime file label: {label}")
        sources[label] = path

    files, external = {}, set()
    pending = list(sources)
    while pending:
        relative = pending.pop()
        if relative in files:
            continue
        path = sources[relative].resolve()
        _no_env(path.parts)
        if relative.startswith("src/") and not path.is_relative_to(root):
            raise ResearchControlError("Local runtime source escapes the source root")
        data = path.read_bytes()
        files[relative] = {"sha256": _byte_hash(data), "bytes": len(data)}
        if not relative.endswith(".py"):
            continue
        try:
            tree = ast.parse(data, filename=relative)
        except (SyntaxError, UnicodeError) as exc:
            raise ResearchControlError(f"Runtime source cannot be parsed: {relative}") from exc
        package = relative.removeprefix("src/").removesuffix(".py").split("/")[:-1]
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = package[:len(package) - node.level + 1] if node.level else []
                module = ".".join([*base, *(node.module.split(".") if node.module else [])])
                modules.append(module)
                modules.extend(module + "." + alias.name for alias in node.names if alias.name != "*")
        for module in modules:
            top = module.split(".")[0]
            if top == "buildtrace":
                parts = module.split(".")
                for count in range(1, len(parts) + 1):
                    found = _module_file(root, ".".join(parts[:count]))
                    if found and found not in sources:
                        sources[found] = _local_file(root, found)
                        pending.append(found)
            elif top and top not in sys.stdlib_module_names and top != "__future__":
                external.add(top)
    return dict(sorted(files.items())), external


def _distribution_sources(distribution) -> dict:
    top_levels = {
        line.strip() for line in (distribution.read_text("top_level.txt") or "").splitlines()
        if line.strip().isidentifier()
    }
    for item in distribution.files or []:
        path = PurePosixPath(str(item).replace("\\", "/"))
        if ".." in path.parts or not path.parts:
            continue
        top = path.parts[0] if len(path.parts) > 1 else path.name.split(".")[0]
        if top.isidentifier() and path.suffix in {".py", ".pyd", ".so"}:
            top_levels.add(top)
    files = {}
    for top in sorted(top_levels):
        try:
            module = importlib.util.find_spec(top)
        except (ImportError, ValueError, AttributeError) as exc:
            raise ResearchControlError(f"Cannot locate effective dependency source: {top}") from exc
        if module is None:
            continue
        if module.submodule_search_locations:
            for location in module.submodule_search_locations:
                root = Path(location).resolve()
                for path in sorted(root.rglob("*")):
                    relative = path.relative_to(root)
                    if any(part == "__pycache__" or part.startswith(".") for part in relative.parts):
                        continue
                    if path.is_file() and path.suffix.casefold() in _SOURCE_SUFFIXES:
                        resolved = path.resolve()
                        _no_env(resolved.parts)
                        if not resolved.is_relative_to(root):
                            raise ResearchControlError("Dependency source escapes its package root")
                        label = f"{top}/{relative.as_posix()}"
                        digest = _byte_hash(resolved.read_bytes())
                        if label in files and files[label] != digest:
                            raise ResearchControlError("Ambiguous effective namespace dependency source")
                        files[label] = digest
        elif module.origin and Path(module.origin).is_file():
            path = Path(module.origin).resolve()
            _no_env(path.parts)
            files[path.name] = _byte_hash(path.read_bytes())
    if not files:
        raise ResearchControlError(f"No effective source found for dependency {distribution.metadata['Name']}")
    return dict(sorted(files.items()))


def _dependency_snapshot(names: Iterable[str]) -> dict:
    """Pin installed runtime dependencies and hash their effective package sources.

    Optional extras are roots only when actually imported by the local closure.
    Installed conditional dependencies are conservatively included. A missing
    conditional dependency is recorded; an absent unconditional dependency fails.
    No installer, VCS command, or dependency import is run.
    """
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    roots = sorted({normalize(name) for name in names})
    pending, result = roots[:], {}
    while pending:
        name = pending.pop()
        if name in result:
            continue
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ResearchControlError(f"Missing pinned runtime dependency: {name}") from exc
        requirements, unavailable = set(), set()
        for requirement in distribution.requires or []:
            if re.search(r"\bextra\s*(?:==|!=|in\b|not\s+in\b)", requirement):
                continue
            match = _REQUIREMENT_NAME.match(requirement)
            if not match:
                raise ResearchControlError(f"Unrecognized dependency requirement for {name}")
            dependency = normalize(match[1])
            try:
                importlib.metadata.distribution(dependency)
            except importlib.metadata.PackageNotFoundError as exc:
                if ";" not in requirement:
                    raise ResearchControlError(f"Missing pinned runtime dependency: {dependency}") from exc
                unavailable.add(dependency)
                continue
            requirements.add(dependency)
            pending.append(dependency)
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        provenance = {}
        if direct.get("vcs_info"):
            info = direct["vcs_info"]
            revision = _text(info.get("commit_id"), f"{name} pinned VCS revision")
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
                raise ResearchControlError("Dependency VCS origins must identify exact revisions")
            provenance = {"vcs": info.get("vcs"), "revision": revision}
            url = urlsplit(direct.get("url", ""))
            if url.scheme and url.scheme != "file" and url.hostname:
                provenance["repository"] = urlunsplit((url.scheme, url.hostname + (f":{url.port}" if url.port else ""), url.path, "", ""))
        elif direct.get("dir_info", {}).get("editable"):
            provenance = {"editable": True}
        files = _distribution_sources(distribution)
        result[name] = {
            "version": distribution.version, "provenance": provenance,
            "dependencies": sorted(requirements), "uninstalled_conditional_dependencies": sorted(unavailable),
            "files": files, "files_sha256": content_sha256(files),
        }
    return {"roots": roots, "distributions": dict(sorted(result.items()))}


def _interface_snapshot() -> dict:
    prompts = importlib.import_module("buildtrace.experiments.prompts")
    values = {
        name: _plain(value) for name, value in vars(prompts).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, (str, dict, list, tuple))
    }
    if not values or not callable(getattr(prompts, "tool_definitions", None)):
        raise ResearchControlError("The shared prompt/tool interface cannot be identified")
    shared = _plain(prompts.tool_definitions(False))
    history = _plain(prompts.tool_definitions(True))
    return {
        "shared_prompts_sha256": content_sha256(values),
        "prompt_hashes": {name: content_sha256(value) for name, value in sorted(values.items())},
        "shared_tools_sha256": content_sha256(shared),
        "tool_hashes": {mode: content_sha256(history if mode == "wikitrace" else shared) for mode in MODES},
    }


def create_runtime_manifest(*, extra_files=(), source_root: Path | str | None = None) -> dict:
    """Hash the explicit on-disk closure, including dirty and untracked sources.

    Extra files can be root-relative paths or ``{portable_label: local_path}``.
    This is an explicit closure, not an arbitrary workspace or Git-status hash.
    """
    root = Path(source_root).resolve() if source_root is not None else _PROJECT_ROOT
    files, external = _source_files(root, extra_files)
    ownership = importlib.metadata.packages_distributions()
    dependencies = set()
    for module in external:
        owners = ownership.get(module)
        if not owners:
            raise ResearchControlError(f"No installed dependency owns runtime import {module}")
        dependencies.update(owners)
    project = tomllib.loads(_local_file(root, "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in project.get("project", {}).get("dependencies", []):
        match = _REQUIREMENT_NAME.match(requirement)
        if not match:
            raise ResearchControlError("Cannot pin a project dependency declaration")
        dependencies.add(match[1])
    return _seal(
        "research-runtime", closure="experiments-python-plus-static-local-imports-and-explicit-files-v2",
        files=files, dependencies=_dependency_snapshot(dependencies), interfaces=_interface_snapshot(),
        python={"implementation": platform.python_implementation(), "version": list(sys.version_info[:3])},
    )


def _spec(value) -> dict:
    value = _plain(value)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 2:
        raise ResearchControlError("Research controls require schema_version 2")
    if not isinstance(value.get("tasks"), list) or not value["tasks"]:
        raise ResearchControlError("Declare research tasks")
    if not isinstance(value.get("retrieval"), dict) or not value["retrieval"]:
        raise ResearchControlError("Declare the deterministic retrieval/selector settings")
    if not isinstance(value.get("model"), dict) or not isinstance(value.get("budget"), dict):
        raise ResearchControlError("Declare model settings and a common budget")
    if value.get("hold_out_families", True) is not True:
        raise ResearchControlError("Research-v2 requires specific-family separation across all splits")
    _integer(value.get("trials"), "repeat count", 3)
    if type(value.get("seed")) is not int:
        raise ResearchControlError("Declare an integer schedule seed")
    _text(value.get("id"), "experiment ID")
    return value


def _training_ledger(manifest: dict) -> list[dict]:
    sources = manifest.get("training_tasks", manifest.get("training_ledger"))
    if not isinstance(sources, list) or not sources:
        raise ResearchControlError("A source corpus needs its actual training-session ledger")
    ledger, run_ids = [], set()
    for index, source in enumerate(sources):
        identity = _identity({**source, "split": "train"})
        run_id = _text(source.get("source_run_id"), "training source run ID")
        if run_id in run_ids:
            raise ResearchControlError("Duplicate training source run ID")
        run_ids.add(run_id)
        row = {**identity, "source_run_id": run_id, "source_step": source.get("source_step", f"step-{index:04d}")}
        if not _IDENTIFIER.fullmatch(row["source_step"]):
            raise ResearchControlError("Invalid training source step")
        for name in ("record_sha256", "task_sha256", "phase_manifest_sha256", "outcome"):
            if name in source:
                row[name] = source[name]
                if name.endswith("sha256"):
                    _digest(row[name], name)
        ledger.append(row)
    _portable(ledger)
    return ledger


def _memory_manifest(memory) -> dict:
    if isinstance(memory, Mapping):
        result = memory.get("manifest", memory)
    else:
        result = getattr(memory, "manifest", None)
    result = _plain(result)
    if not isinstance(result, dict):
        raise ResearchControlError("Supply the frozen source corpus, including its manifest")
    return result


def _corpus(spec: dict, memory, *, verify=False) -> dict | None:
    expected, insights = spec.get("corpus_sha256"), spec.get("insights_sha256")
    if memory is None:
        if expected is not None or insights is not None:
            raise ResearchControlError("Supply memory to bind the source corpus and training ledger")
        return None
    _digest(expected, "source corpus")
    _digest(insights, "injected insights")
    manifest = _memory_manifest(memory)
    if content_sha256(manifest) != expected or getattr(memory, "sha256", expected) != expected:
        raise ResearchControlError("The source corpus differs from its declared content address")
    if manifest.get("insights_sha256") != insights or getattr(memory, "insights_sha256", insights) != insights:
        raise ResearchControlError("The source corpus has different injected insights")
    if manifest.get("data_kind") == "synthetic" and spec.get("data_kind") == "live":
        raise ResearchControlError("Synthetic source histories cannot be presented as live histories")
    ledger = _training_ledger(manifest)
    steps = {row["source_step"]: row["source_run_id"] for row in ledger}
    if len(steps) != len(ledger):
        raise ResearchControlError("Training source steps must be unique")
    links = {}
    for item in manifest.get("insights", []):
        identifier, step = _text(item.get("id"), "insight ID"), item.get("source_step")
        if identifier in links or step not in steps:
            raise ResearchControlError("Insights must link uniquely to actual training sessions")
        links[identifier] = {"source_step": step, "source_run_id": steps[step]}
    if not links or not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        raise ResearchControlError("The source corpus needs linked insights and immutable files")
    files = {_relative(path): _digest(digest, "corpus file") for path, digest in manifest["files"].items()}
    if verify:
        reader = _source_reader(memory, None)
        for relative, digest in files.items():
            if _byte_hash(reader(relative)) != digest:
                raise ResearchControlError(f"Source corpus file was modified: {relative}")
    return {
        "corpus_sha256": expected, "insights_sha256": insights,
        "training_ledger": ledger, "training_ledger_sha256": content_sha256(ledger),
        "insight_links": dict(sorted(links.items())), "files": dict(sorted(files.items())),
        "upstream": manifest.get("upstream"), "compiler_model": manifest.get("compiler_model"),
        "data_kind": manifest.get("data_kind"),
    }


def _schedule_policy(spec: dict) -> dict:
    return {
        "id": SCHEDULE_POLICY, "seed": spec["seed"], "repeats": spec["trials"],
        "evaluation_modes": list(MODES), "training_modes": ["no-memory"],
        "roles": ["reference"], "task_order": "sort-id-then-python-Random-shuffle",
        "mode_order": "shuffle-once-per-task-then-left-rotate-by-trial",
    }


def seed_schedule(spec, split: str) -> list[dict]:
    """The v2 reference schedule: all three modes on both dev and test."""
    spec = _spec(spec)
    if split not in SPLITS:
        raise ResearchControlError("Choose train, dev, or test")
    tasks = sorted((task for task in spec["tasks"] if task["split"] == split), key=lambda task: task["id"])
    rng = random.Random(spec["seed"])
    rng.shuffle(tasks)
    result = []
    for task in tasks:
        modes = list(MODES) if split != "train" else ["no-memory"]
        rng.shuffle(modes)
        for trial in range(spec["trials"]):
            ordered = modes[trial % len(modes):] + modes[:trial % len(modes)]
            result.extend({"task_id": task["id"], "mode": mode, "trial": trial, "role": "reference"} for mode in ordered)
    return result


def _active_inputs(spec: dict, split: str, gate_task_ids=None) -> dict:
    if split not in SPLITS:
        raise ResearchControlError("Choose train, dev, or test")
    tasks = sorted((task for task in spec["tasks"] if task["split"] == split), key=lambda task: task["id"])
    if not tasks or len({task["id"] for task in tasks}) != len(tasks):
        raise ResearchControlError("The active split needs uniquely identified tasks")
    captures, entries = [], []
    for task in tasks:
        _identity(task)
        _portable(task)
        _digest(task.get("capture_sha256"), "active task capture")
        if not isinstance(task.get("capture_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", task["capture_id"]):
            raise ResearchControlError("Pin the active task's capture ID")
        if not isinstance(task.get("oracle_sha"), str) or not _REVISION.fullmatch(task["oracle_sha"]):
            raise ResearchControlError("Pin the active task's trusted verifier SHA before creating a candidate")
        _integer(task.get("expected_test_count"), "original expected test count", 1)
        entries.append({"id": task["id"], "sha256": content_sha256(task), "task": task})
        captures.append({"task_id": task["id"], "capture_id": task["capture_id"], "capture_sha256": task["capture_sha256"]})
    selected = sorted(task["id"] for task in tasks) if split == "dev" and gate_task_ids is None else list(gate_task_ids or [])
    if len(selected) != len(set(selected)) or not set(selected) <= {task["id"] for task in tasks}:
        raise ResearchControlError("Predeclare distinct gate task IDs from the active development split")
    if split != "dev" and selected:
        raise ResearchControlError("Mechanism gate tasks belong only to the development candidate")
    schedule = seed_schedule(spec, split)
    return {
        "tasks": entries, "task_manifest_sha256": content_sha256(entries),
        "captures": captures, "capture_manifest_sha256": content_sha256(captures),
        "schedule": schedule, "schedule_sha256": content_sha256(schedule), "gate_task_ids": sorted(selected),
    }


def _spec_controls(spec: dict) -> dict:
    excluded = {
        "schema_version", "id", "tasks", "corpus", "corpus_sha256", "insights_sha256",
        "split_registry", "phase_manifest_sha256", "model", "budget", "retrieval", "trials", "seed",
    }
    result = {name: value for name, value in spec.items() if name not in excluded}
    _portable(result)
    return result


def create_input_manifest(
    spec, split: str, memory=None, registry=None, extra_files=(), *,
    gate_task_ids: Sequence[str] | None = None, source_root: Path | str | None = None,
) -> dict:
    """Create a candidate identity, not an authorization to start held-out testing.

    Only active tasks/captures are included. The registry includes cross-spec
    identities, never inactive capture/verifier contents. ``phase_manifest_sha256``
    and the machine-local corpus path are excluded, preventing hash cycles.
    """
    spec = _spec(spec)
    active = _active_inputs(spec, split, gate_task_ids)
    corpus = _corpus(spec, memory, verify=True)
    if split != "train" and corpus is None:
        raise ResearchControlError("Development and evaluation need a frozen training corpus")
    identities = [*spec["tasks"], *(corpus["training_ledger"] if corpus else [])]
    merged = create_split_registry(spec.get("split_registry", []), registry)
    merged = create_split_registry(identities, merged)
    protocol = {
        "runtime": create_runtime_manifest(extra_files=extra_files, source_root=source_root),
        "model": spec["model"], "budget": spec["budget"], "retrieval": spec["retrieval"],
        "source_corpus": corpus, "seed_schedule_policy": _schedule_policy(spec),
        "development_gate_policy_sha256": content_sha256(GATE_POLICY), "controls": _spec_controls(spec),
    }
    _portable(protocol)
    return _seal(
        "research-input-manifest", phase={"train": "training-candidate", "dev": "development-candidate", "test": "evaluation-candidate"}[split],
        split=split, experiment_id=spec["id"], protocol=protocol, protocol_sha256=content_sha256(protocol),
        split_registry=merged, split_registry_sha256=merged["sha256"], active=active,
    )


def _check_input_manifest(manifest) -> dict:
    manifest = _check_seal(manifest, "research-input-manifest")
    if manifest.get("split") not in SPLITS or manifest.get("protocol_sha256") != content_sha256(manifest.get("protocol")):
        raise ResearchControlError("Invalid candidate protocol identity")
    if manifest["protocol"].get("development_gate_policy_sha256") != content_sha256(GATE_POLICY):
        raise ResearchControlError("The candidate names a different development gate policy")
    _check_seal(manifest["protocol"]["runtime"], "research-runtime")
    if verify_split_registry(manifest["split_registry"]) != manifest.get("split_registry_sha256"):
        raise ResearchControlError("Candidate registry identity mismatch")
    active = manifest.get("active", {})
    for field, digest in (("tasks", "task_manifest_sha256"), ("captures", "capture_manifest_sha256"), ("schedule", "schedule_sha256")):
        if active.get(digest) != content_sha256(active.get(field)):
            raise ResearchControlError(f"Candidate {field} identity mismatch")
    for entry in active["tasks"]:
        if entry.get("id") != entry.get("task", {}).get("id") or entry.get("sha256") != content_sha256(entry.get("task")):
            raise ResearchControlError("Candidate task identity mismatch")
    return manifest


def verify_input_manifest(manifest, spec, split: str | None = None, memory=None, registry=None, extra_files=(), *, source_root=None) -> str:
    """Recompute the effective closure and active inputs; return their checked SHA."""
    manifest = _check_input_manifest(manifest)
    selected = manifest["active"]["gate_task_ids"]
    fresh = create_input_manifest(
        spec, split or manifest["split"], memory, registry, extra_files,
        gate_task_ids=selected, source_root=source_root,
    )
    if fresh != manifest:
        raise ResearchControlError("Candidate inputs changed: source, dependencies, protocol, registry, or active split")
    return manifest["sha256"]


def _source_reader(memory, resolver: Callable[[str], bytes] | None) -> Callable[[str], bytes]:
    if resolver is not None:
        reader = resolver
    elif callable(getattr(memory, "read_source", None)):
        reader = memory.read_source
    elif getattr(memory, "directory", None) is not None:
        root = Path(memory.directory).resolve()
        reader = lambda relative: _local_file(root, relative).read_bytes()
    else:
        raise ResearchControlError("Supply memory.directory/read_source or a corpus-relative bytes source_resolver")

    def checked(relative):
        _relative(relative)
        try:
            content = reader(relative)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            raise ResearchControlError(f"The source resolver could not replay {relative}") from exc
        if not isinstance(content, bytes):
            raise ResearchControlError("The source resolver must return original document bytes")
        return content

    return checked


def _canonical_document(read: dict, links: dict) -> str:
    insight = read.get("insight_id")
    if insight not in links:
        raise ResearchControlError("The read is not linked to a frozen insight/training session")
    step = links[insight]["source_step"]
    document = read.get("document", "")
    if not isinstance(document, str):
        raise ResearchControlError("Invalid delivered document")
    if document.startswith("/work/corpus/"):
        document = document[len("/work/corpus/"):]
    if read.get("layer") == "raw":
        document = document or "metadata.md"
        if not document.startswith("raw/"):
            document = f"raw/{step}/{document}"
        if not document.startswith(f"raw/{step}/"):
            raise ResearchControlError("Raw evidence belongs to another insight's source run")
    elif read.get("layer") == "compilation":
        expected = f"compilation/{step}.md"
        if document and document != expected:
            raise ResearchControlError("Compilation evidence belongs to another source run")
        document = expected
    else:
        raise ResearchControlError("Read raw or compilation evidence")
    return _relative(document)


def _json_fields(text: str) -> dict:
    """JSON object fields and their exact value spans, rejecting duplicate keys."""
    decoder = json.JSONDecoder()
    position, fields = 0, {}

    def whitespace(index):
        while index < len(text) and text[index].isspace():
            index += 1
        return index

    position = whitespace(position)
    if position >= len(text) or text[position] != "{":
        raise ResearchControlError("A recorded tool observation must be a JSON object")
    position = whitespace(position + 1)
    while position < len(text) and text[position] != "}":
        key, position = decoder.raw_decode(text, position)
        position = whitespace(position)
        if not isinstance(key, str) or key in fields or position >= len(text) or text[position] != ":":
            raise ResearchControlError("Ambiguous recorded tool observation fields")
        start = whitespace(position + 1)
        value, end = decoder.raw_decode(text, start)
        fields[key] = (value, start, end)
        position = whitespace(end)
        if position < len(text) and text[position] == "}":
            break
        if position >= len(text) or text[position] != ",":
            raise ResearchControlError("Invalid recorded tool observation")
        position = whitespace(position + 1)
        if position < len(text) and text[position] == "}":
            raise ResearchControlError("Trailing comma in recorded tool observation")
    if position >= len(text) or text[position] != "}" or whitespace(position + 1) != len(text):
        raise ResearchControlError("Invalid recorded tool observation")
    return fields


def _journal_observation(text: str, offset: int, count: int) -> bool:
    """Only delivered successful current-file/log tool payloads are observations.

    Merely delivering a journal's call header, plan, assistant summary, request
    error, directory listing, or patch-verification result does not qualify.
    """
    position = 0
    for line in text.splitlines(keepends=True):
        end = position + len(line)
        if position < offset + count and offset < end and line.strip():
            try:
                fields = _json_fields(line)
                row = {key: item[0] for key, item in fields.items()}
                name = row.get("name")
                if row.get("type") != "function_call" or row.get("is_error") is not False or name not in {"read_current_file", "read_current_log"}:
                    position = end
                    continue
                result, start, stop = fields["result"]
                if not isinstance(result, dict) or result.get("error"):
                    position = end
                    continue
                payloads = _json_fields(line[start:stop])
                payload, first, last = payloads["content" if name == "read_current_file" else "text"]
                if isinstance(payload, str) and payload.strip():
                    # Exclude the JSON quotes themselves from the overlap.
                    first, last = position + start + first + 1, position + start + last - 1
                    overlap = text[max(offset, first):min(offset + count, last)]
                    if overlap.strip() and re.sub(r"\\(?:[nrt]|u00(?:09|0[aAdD]|20))", "", overlap).strip():
                        return True
            except (ResearchControlError, ValueError, KeyError, TypeError):
                pass
        position = end
    return False


def _replay_read(read: dict, corpus: dict, reader: Callable[[str], bytes], *, include_excerpt=False) -> dict:
    if read.get("status") != "delivered" or read.get("error") not in (None, ""):
        raise ResearchControlError("A request error is not an evidence delivery")
    _text(read.get("question"), "evidence question")
    if read.get("trigger") not in _TRIGGERS:
        raise ResearchControlError("Invalid evidence trigger")
    relative = _canonical_document(read, corpus["insight_links"])

    def original(path):
        expected = corpus["files"].get(path)
        if expected is None:
            raise ResearchControlError("Document is outside the frozen linked corpus")
        content = reader(path)
        if _byte_hash(content) != expected:
            raise ResearchControlError("Replayed document checksum mismatch")
        return content

    content = original(relative)
    if read.get("document_sha256") != _byte_hash(content):
        raise ResearchControlError("Delivered document identity does not replay")
    text = content.decode("utf-8", errors="replace")
    offset = _integer(read.get("offset"), "delivered offset")
    count = _integer(read.get("delivered_characters"), "delivered character count", 1)
    excerpt = text[offset:offset + count]
    if len(excerpt) != count or not excerpt.strip():
        raise ResearchControlError("An empty, blank, or out-of-range excerpt is not an observation")
    if read.get("excerpt_sha256") != _byte_hash(excerpt.encode("utf-8")):
        raise ResearchControlError("Delivered excerpt identity does not replay")
    first = text[:offset].count("\n") + 1
    last = first + excerpt.count("\n")
    if _integer(read.get("first_line"), "first delivered line", 1) != first or _integer(read.get("last_line"), "last delivered line", 1) != last:
        raise ResearchControlError("Delivered excerpt line range does not replay")

    eligible = False
    name = PurePosixPath(relative).name.casefold()
    source_root = f"raw/{corpus['insight_links'][read['insight_id']]['source_step']}"
    if read["layer"] == "raw" and relative == source_root + "/solver.full.jsonl":
        eligible = _journal_observation(text, offset, count)
    elif read["layer"] == "raw" and name not in _NON_OBSERVATIONS:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != source_root and len(parent.parts) > 2:
            manifest_path = (parent / "manifest.json").as_posix()
            if manifest_path in corpus["files"]:
                try:
                    capture = json.loads(original(manifest_path))
                except (ValueError, UnicodeError) as exc:
                    raise ResearchControlError("Original capture manifest is not replayable JSON") from exc
                if not isinstance(capture, dict) or capture.get("schema_version") != 1:
                    raise ResearchControlError("Original capture manifest has an unsupported shape")
                if parent.as_posix() == source_root + "/initial-ci":
                    source_step = corpus["insight_links"][read["insight_id"]]["source_step"]
                    source = next(row for row in corpus["training_ledger"] if row["source_step"] == source_step)
                    if capture.get("source_ref") != source["base_sha"]:
                        raise ResearchControlError("The original capture belongs to a different training snapshot")
                local_path = PurePosixPath(relative).relative_to(parent).as_posix()
                entries = [entry for entry in capture.get("entries", []) if entry.get("path") == local_path]
                if len(entries) != 1 or entries[0].get("sha256") != _byte_hash(content):
                    raise ResearchControlError("Original observation is not bound to its capture entry")
                eligible = entries[0].get("kind") in {"log", "source", "artifact"}
                break
            parent = parent.parent
    replay = {
        "read_id": read["id"], "insight_id": read["insight_id"], "layer": read["layer"],
        "document": relative, "document_sha256": _byte_hash(content),
        "excerpt_sha256": read["excerpt_sha256"], "offset": offset, "delivered_characters": count,
        "first_line": first, "last_line": last, "original_observation": eligible,
        "source_run_id": corpus["insight_links"][read["insight_id"]]["source_step"],
    }
    if include_excerpt:
        replay["excerpt"] = excerpt
    return replay


def _decision_core(decision) -> dict:
    if not isinstance(decision, dict) or decision.get("action") not in {"investigate", "apply", "adapt", "reject", "patch", "stop"}:
        raise ResearchControlError("Missing actionable decision")
    if not isinstance(decision.get("parameters"), dict):
        raise ResearchControlError("Decision parameters must be an object")
    return {"action": decision["action"], "target": _text(decision.get("target"), "decision target"), "parameters": decision["parameters"]}


def _action_index(run: dict) -> tuple[list[dict], dict, dict]:
    actions, reads = run.get("actions", []), run.get("evidence_reads", [])
    if not isinstance(actions, list) or not isinstance(reads, list):
        raise ResearchControlError("Actions and evidence deliveries must be lists")
    by_id, by_sequence, read_ids, patch_attempts = {}, set(), {}, set()
    for action in actions:
        identifier = _text(action.get("id"), "action ID")
        sequence = _integer(action.get("sequence"), "action sequence")
        if identifier in by_id or sequence in by_sequence or action.get("kind") not in {"plan", "current-observation", "history-read", "patch"}:
            raise ResearchControlError("Duplicate or invalid observable action identity/order")
        if not isinstance(action.get("paths"), list) or any(not isinstance(path, str) for path in action["paths"]):
            raise ResearchControlError("Action paths must be recorded")
        for path in action["paths"]:
            _relative(path)
        if action["kind"] == "patch" and action.get("verification_attempt") is not None:
            attempt = _integer(action["verification_attempt"], "patch verification attempt", 1)
            if attempt in patch_attempts:
                raise ResearchControlError("Two patch actions cannot claim the same verification attempt")
            patch_attempts.add(attempt)
        by_id[identifier] = action
        by_sequence.add(sequence)
    for read in reads:
        identifier = _text(read.get("id"), "evidence read ID")
        action = by_id.get(identifier)
        if identifier in read_ids or action is None or action["kind"] != "history-read" or type(read.get("sequence")) is not int or read["sequence"] != action["sequence"]:
            raise ResearchControlError("Evidence read IDs/sequences must replay one-to-one as history-read actions")
        read_ids[identifier] = read
    if {action["id"] for action in actions if action["kind"] == "history-read"} != set(read_ids):
        raise ResearchControlError("A history-read action has no recorded delivery/error")
    return sorted(actions, key=lambda action: action["sequence"]), by_id, read_ids


def _passing_verification(verification) -> bool:
    return (
        isinstance(verification, dict)
        and verification.get("workflow_conclusion") == "success"
        and all(verification.get(field) is True for field in ("required_checks_passed", "protected_files_unchanged", "oracle_passed"))
        and isinstance(verification.get("candidate_sha"), str)
        and _REVISION.fullmatch(verification["candidate_sha"]) is not None
    )


def _reviewer_independent(reviewer, allowed: set[str], solver_ids: set[str], run: dict) -> bool:
    actors = solver_ids | {
        str(value).strip().casefold() for value in (
            run.get("solver_id"), run.get("agent_id"), run.get("model", {}).get("model"),
            *run.get("returned_models", []),
        ) if value
    }
    return isinstance(reviewer, str) and reviewer.strip().casefold() in allowed - actors


def _plan_paths(decision: dict) -> set[str]:
    """Explicit path targets, not arbitrary strings buried in an explanation."""
    paths = set()
    target = decision["target"]
    if "/" in target or PurePosixPath(target).suffix or target in {"*", "**"}:
        paths.add(_relative(target))
    for name in ("path", "file", "paths", "files"):
        values = decision["parameters"].get(name, [])
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            raise ResearchControlError("Plan path parameters must be paths or lists of paths")
        paths.update(_relative(value) for value in values)
    return paths


def _chain(run: dict, event: dict, stream, corpus: dict, reader, reviewers: set[str], solver_ids: set[str]) -> dict:
    actions, by_id, reads = stream
    if event.get("contaminated") is not False:
        raise ResearchControlError("The evidence burst is contaminated or not assessed")
    if event.get("material_effect") not in {"change", "refinement"} or event.get("adoption") != "adopted":
        raise ResearchControlError("The plan has no reviewed material effect and observable adoption")
    if not _reviewer_independent(event.get("mechanism_reviewer"), reviewers, solver_ids, run):
        raise ResearchControlError("An authorized independent mechanism reviewer is required")
    before, after = by_id.get(event.get("before_action_id")), by_id.get(event.get("after_action_id"))
    if before is None or after is None or before["kind"] != "plan" or after["kind"] != "plan":
        raise ResearchControlError("Before/after action IDs do not replay as plans")
    if before.get("decision") != event.get("before") or after.get("decision") != event.get("after"):
        raise ResearchControlError("Before/after decision snapshots do not match their action IDs")
    if _decision_core(before.get("decision")) == _decision_core(after.get("decision")):
        raise ResearchControlError("Wording-only changes are not material changes/refinements")
    read_ids = event.get("read_ids")
    if not isinstance(read_ids, list) or not read_ids or len(read_ids) != len(set(read_ids)) or any(identifier not in reads for identifier in read_ids):
        raise ResearchControlError("The evidence burst needs distinct recorded read IDs")
    between = [action for action in actions if before["sequence"] < action["sequence"] < after["sequence"]]
    if [action["id"] for action in between] != read_ids or any(action["kind"] != "history-read" for action in between):
        raise ResearchControlError("Before/after plans are not fresh: intervening or unlinked actions")
    packet = run.get("packet_insight_ids", [])
    if any(reads[identifier].get("insight_id") not in packet for identifier in read_ids):
        raise ResearchControlError("Evidence was not linked from the run's selected insight packet")
    if event.get("question") != reads[read_ids[0]].get("question") or event.get("trigger") != reads[read_ids[0]].get("trigger"):
        raise ResearchControlError("Evidence burst question/trigger does not match its first read")
    replayed = [_replay_read(reads[identifier], corpus, reader) for identifier in read_ids]
    if not any(read["original_observation"] for read in replayed):
        raise ResearchControlError("Metadata, verification, compilations, and solver summaries alone are not original observations")
    citations = event.get("citations", [])
    if not citations:
        raise ResearchControlError("An exact replayed citation is required")

    def matches(citation, read):
        return (
            citation.get("insight_id") == read["insight_id"]
            and citation.get("source_run_id") == read["source_run_id"]
            and citation.get("layer") == read["layer"]
            and citation.get("document") in {read["document"], "/work/corpus/" + read["document"]}
            and all(citation.get(field) == read[field] for field in ("document_sha256", "first_line", "last_line"))
        )

    if any(not any(matches(citation, read) for citation in citations) for read in replayed) or any(not any(matches(citation, read) for read in replayed) for citation in citations):
        raise ResearchControlError("Citations do not identify exactly the actually delivered excerpts")
    adoption_ids = event.get("adoption_action_ids")
    if not isinstance(adoption_ids, list) or not adoption_ids or len(adoption_ids) != len(set(adoption_ids)):
        raise ResearchControlError("Observable adoption action IDs are required")
    adopted = []
    targets = _plan_paths(after["decision"])
    if not targets:
        raise ResearchControlError("The adopted plan needs an explicit target path or path/files parameters")
    for identifier in adoption_ids:
        action = by_id.get(identifier)
        if action is None or action["sequence"] <= after["sequence"] or action["kind"] not in {"current-observation", "patch"} or not action["paths"]:
            raise ResearchControlError("Adoption IDs do not replay as subsequent observable actions/patches")
        if _decision_core(action.get("decision")) != _decision_core(after["decision"]):
            raise ResearchControlError("The subsequent action did not adopt the reviewed plan")
        if not any(fnmatch.fnmatchcase(path, target) for path in action["paths"] for target in targets):
            raise ResearchControlError("The subsequent action's paths do not implement/investigate the reviewed target")
        if any(after["sequence"] < other["sequence"] < action["sequence"] and other["kind"] in {"plan", "history-read"} for other in actions):
            raise ResearchControlError("A new plan/history burst intervened before the claimed adoption")
        if action["kind"] == "patch":
            attempt = _integer(action.get("verification_attempt"), "adopted patch verification attempt", 1)
            if not any(verification.get("attempt") == attempt for verification in run.get("verifications", [])):
                raise ResearchControlError("The adopted patch has no corresponding verification attempt")
        adopted.append(action)
    final = run.get("verifications", [])[-1:]
    patches = [action for action in actions if action["kind"] == "patch"]
    last_patch = patches[-1] if patches else None
    repaired = bool(
        run.get("outcome") == "solved" and final and _passing_verification(final[0])
        and last_patch and last_patch["id"] in adoption_ids
        and last_patch.get("verification_attempt") == final[0].get("attempt")
    )
    return {
        "run_id": run["id"], "event_id": event["id"], "trial": run["trial"],
        "read_ids": read_ids, "replayed_reads": replayed, "adoption_action_ids": adoption_ids,
        "material_effect": event["material_effect"], "mechanism_reviewer": event["mechanism_reviewer"],
        "verified_repair": repaired,
    }


def _records_digest(records: list[dict]) -> str:
    # The observable record, not a machine-local journal pathname, is committed.
    portable = [{key: value for key, value in row.items() if key != "trajectory_path"} for row in records]
    return content_sha256(sorted(portable, key=lambda row: (str(row.get("id", "")), content_sha256(row))))


def _record_rows(records: Iterable) -> list[dict]:
    rows = [_plain(record) for record in records]
    for run in rows:
        if not isinstance(run, dict):
            raise ResearchControlError("Run records must be objects")
        for field in ("id", "task_id", "mode", "role", "split", "outcome"):
            if run.get(field) is not None and not isinstance(run[field], str):
                raise ResearchControlError(f"Run {field} must be a string")
        if type(run.get("trial")) is not int:
            raise ResearchControlError("Run trial must be an integer")
        for field in ("actions", "evidence_reads", "evidence_decisions", "verifications", "hunks"):
            if not isinstance(run.get(field, []), list) or any(not isinstance(item, dict) for item in run.get(field, [])):
                raise ResearchControlError(f"Run {field} must be a list of objects")
        if not isinstance(run.get("returned_models", []), list) or any(not isinstance(item, str) for item in run.get("returned_models", [])):
            raise ResearchControlError("Served model identities must be a list of strings")
    return rows


def _identity_issues(records: list[dict], manifest: dict, phase_sha256: str) -> list[str]:
    issues, ids, cells, packets = [], set(), set(), {}
    protocol = manifest["protocol"]
    tasks = {entry["id"]: entry for entry in manifest["active"]["tasks"]}
    corpus = protocol["source_corpus"]
    for run in records:
        identifier = run.get("id")
        label = str(identifier or "<missing-run-id>")
        cell = (run.get("task_id"), run.get("mode"), run.get("trial"), run.get("role", "reference"))
        if not isinstance(identifier, str) or not identifier or identifier in ids or cell in cells:
            issues.append(f"{label}: duplicate or missing run/cell identity")
        ids.add(identifier)
        cells.add(cell)
        task = tasks.get(run.get("task_id"))
        if type(run.get("schema_version")) is not int or run["schema_version"] != 2 or run.get("split") != manifest["split"] or run.get("phase_manifest_sha256") != phase_sha256:
            issues.append(f"{label}: wrong schema, split, or phase manifest")
        if run.get("experiment_id") != manifest["experiment_id"] or run.get("protocol_sha256") != manifest["protocol_sha256"]:
            issues.append(f"{label}: different experiment/protocol")
        if task is None or run.get("task_sha256") != task["sha256"]:
            issues.append(f"{label}: different active task snapshot")
        if run.get("model") != protocol["model"] or run.get("budget") != protocol["budget"]:
            issues.append(f"{label}: different model or budget")
        if run.get("mode") not in MODES or type(run.get("trial")) is not int or not 0 <= run["trial"] < protocol["seed_schedule_policy"]["repeats"] or run.get("role", "reference") != "reference":
            issues.append(f"{label}: unexpected mode/repeat/role")
        if run.get("outcome") not in {"solved", "unsolved", "infra-error", "not-reproduced"}:
            issues.append(f"{label}: invalid outcome")
        final = run.get("verifications", [])[-1:]
        attempts = [item.get("attempt") for item in run.get("verifications", [])]
        if any(type(attempt) is not int for attempt in attempts) or attempts != list(range(1, len(attempts) + 1)):
            issues.append(f"{label}: verification attempts are not unique and sequential")
        if (run.get("outcome") == "solved") != bool(final and _passing_verification(final[0])):
            issues.append(f"{label}: outcome does not match final verification")
        if run.get("mode") == "no-memory":
            if any(run.get(field) for field in ("corpus_sha256", "insights_sha256", "packet_sha256", "packet_insight_ids")):
                issues.append(f"{label}: no-memory received historical inputs")
        elif run.get("mode") in MODES:
            if corpus is None or any(run.get(field) != corpus[field] for field in ("corpus_sha256", "insights_sha256")):
                issues.append(f"{label}: different frozen corpus/insights")
            packet = run.get("packet_insight_ids")
            if not isinstance(packet, list) or any(not isinstance(item, str) for item in packet) or len(packet) != len(set(packet)) or corpus is None or any(item not in corpus["insight_links"] for item in packet):
                issues.append(f"{label}: invalid selected insight identities")
            if not isinstance(run.get("packet_sha256"), str) or not _SHA256.fullmatch(run["packet_sha256"]):
                issues.append(f"{label}: missing packet content address")
            signature = (run.get("packet_sha256"), content_sha256(packet))
            if run.get("task_id") in packets and packets[run["task_id"]] != signature:
                issues.append(f"{label}: memory modes/repeats received different selected packets")
            packets[run.get("task_id")] = signature
        if run.get("mode") != "wikitrace" and (
            run.get("evidence_reads") or run.get("evidence_decisions")
            or any(action.get("kind") == "history-read" for action in run.get("actions", []))
        ):
            issues.append(f"{label}: historical source access outside WikiTrace")
    served = {model for run in records for model in run.get("returned_models", [])}
    if len(served) > 1:
        issues.append("The development/evaluation cohort mixes served model versions")
    return issues


def _check_spec_binding(spec: dict, manifest: dict, memory=None) -> None:
    if spec["id"] != manifest["experiment_id"] or _active_inputs(spec, manifest["split"], manifest["active"]["gate_task_ids"]) != manifest["active"]:
        raise ResearchControlError("The candidate has different active tasks, captures, gate tasks, or schedule")
    protocol = manifest["protocol"]
    for field in ("model", "budget", "retrieval"):
        if spec[field] != protocol[field]:
            raise ResearchControlError(f"The candidate has different {field}")
    if _schedule_policy(spec) != protocol["seed_schedule_policy"] or _spec_controls(spec) != protocol["controls"]:
        raise ResearchControlError("The candidate has different protocol controls")
    corpus = protocol["source_corpus"]
    if corpus is None or any(spec.get(field) != corpus[field] for field in ("corpus_sha256", "insights_sha256")):
        raise ResearchControlError("The candidate has different source memory")
    if memory is not None and _corpus(spec, memory) != corpus:
        raise ResearchControlError("The candidate has different source links/training ledger")
    registry = create_split_registry([*spec["tasks"], *spec.get("split_registry", []), *corpus["training_ledger"]], manifest["split_registry"])
    if registry != manifest["split_registry"]:
        raise ResearchControlError("The candidate has a different cross-spec split registry")


def evaluate_development_gate(
    spec, records: Iterable, *, manifest, memory=None,
    source_resolver: Callable[[str], bytes] | None = None,
    independent_reviewers: Iterable[str] = (), solver_identities: Iterable[str] = (),
) -> dict:
    """Pure assessment (apart from the supplied read-only source resolver).

    Bad/missing chains are reported, never promoted by review flags alone. A
    candidate/spec mismatch raises; unsuccessful gate requirements return a
    sealed report with ``passed=False``. No score comparison is performed.
    """
    spec, manifest = _spec(spec), _check_input_manifest(manifest)
    if manifest["split"] != "dev" or manifest.get("phase") != "development-candidate":
        raise ResearchControlError("The mechanism gate requires a development candidate")
    _check_spec_binding(spec, manifest, memory)
    if spec.get("phase_manifest_sha256") not in (None, manifest["sha256"]):
        raise ResearchControlError("The spec points to a different development phase manifest")
    rows = _record_rows(records)
    reader = _source_reader(memory, source_resolver)
    allowed = {_text(name, "independent reviewer").strip().casefold() for name in independent_reviewers}
    solvers = {_text(name, "solver identity").strip().casefold() for name in solver_identities}
    issues = _identity_issues(rows, manifest, manifest["sha256"])
    if spec["trials"] != 3:
        issues.append("The predeclared development gate requires exactly three repeats per mode")
    expected = {(item["task_id"], item["mode"], item["trial"], item["role"]) for item in manifest["active"]["schedule"]}
    present = {(run.get("task_id"), run.get("mode"), run.get("trial"), run.get("role", "reference")) for run in rows}
    if present != expected:
        issues.append(f"Incomplete or unexpected development grid: {len(expected - present)} missing, {len(present - expected)} extra cells")
    selected = manifest["active"]["gate_task_ids"]
    tasks = {entry["id"]: entry["task"] for entry in manifest["active"]["tasks"]}
    families = [tasks[identifier]["family"].casefold() for identifier in selected]
    if len(selected) < 2 or len(set(families)) != len(families) or any(family in _COARSE_FAMILIES for family in families):
        issues.append("Predeclare at least two development tasks from different specific, non-coarse families")
    results = {}
    for task_id in sorted(tasks):
        chains, rejected = [], []
        for run in sorted((row for row in rows if row.get("task_id") == task_id and row.get("mode") == "wikitrace"), key=lambda row: str(row.get("id", ""))):
            events = run.get("evidence_decisions", [])
            try:
                stream = _action_index(run)
                event_ids, used_reads = set(), set()
                for event in events:
                    event_id = _text(event.get("id"), "evidence burst ID")
                    if event_id in event_ids or used_reads.intersection(event.get("read_ids", [])):
                        raise ResearchControlError("Duplicate evidence burst or reused read IDs")
                    event_ids.add(event_id)
                    used_reads.update(event.get("read_ids", []))
            except (ResearchControlError, TypeError, KeyError, AttributeError) as exc:
                rejected.append({"run_id": run.get("id"), "event_id": None, "reason": str(exc)})
                continue
            for event in events:
                try:
                    chains.append(_chain(run, event, stream, manifest["protocol"]["source_corpus"], reader, allowed, solvers))
                except (ResearchControlError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    rejected.append({"run_id": run.get("id"), "event_id": event.get("id"), "reason": str(exc)})
        repeats = sorted({chain["trial"] for chain in chains})
        results[task_id] = {"family": tasks[task_id]["family"], "required_for_gate": task_id in selected, "qualifying_trials": repeats, "chains": chains, "rejected_chains": rejected}
        if task_id in selected and len(repeats) < 2:
            issues.append(f"{task_id}: fewer than two WikiTrace repeats have replayed original observations, material plans, and adoption")
    repair_chains = sum(chain["verified_repair"] for task_id in selected for chain in results[task_id]["chains"])
    if repair_chains < 1:
        issues.append("No qualifying development chain ends in a verified repair")
    return _seal(
        "research-development-gate", candidate_manifest_sha256=manifest["sha256"],
        protocol_sha256=manifest["protocol_sha256"], gate_policy_sha256=content_sha256(GATE_POLICY),
        records_sha256=_records_digest(rows), predeclared_task_ids=selected,
        independent_reviewers=sorted(allowed), solver_identities=sorted(solvers),
        expected_cells=len(expected), observed_records=len(rows), outcome_counts=dict(sorted(Counter(str(run.get("outcome")) for run in rows).items())),
        tasks=results, verified_repair_chains=repair_chains, passed=not issues, failures=issues,
    )


def _check_protocol_freeze(freeze) -> dict:
    freeze = _check_seal(freeze, "research-protocol-freeze")
    gate = _check_seal(freeze.get("development_gate"), "research-development-gate")
    if freeze.get("protocol_sha256") != content_sha256(freeze.get("protocol")):
        raise ResearchControlError("Frozen protocol identity mismatch")
    if gate.get("passed") is not True or gate.get("failures") or gate.get("candidate_manifest_sha256") != freeze.get("development_manifest_sha256") or gate.get("protocol_sha256") != freeze["protocol_sha256"] or gate.get("gate_policy_sha256") != content_sha256(GATE_POLICY):
        raise ResearchControlError("The protocol freeze has no matching passed development gate")
    selected = gate.get("predeclared_task_ids", [])
    if len(selected) < 2 or any(len(gate["tasks"][task_id]["qualifying_trials"]) < 2 for task_id in selected) or gate.get("verified_repair_chains", 0) < 1:
        raise ResearchControlError("The frozen development gate has insufficient mechanism coverage")
    return freeze


def freeze_protocol(
    spec, records: Iterable, *, manifest, memory=None, registry=None, extra_files=(),
    source_resolver=None, independent_reviewers: Iterable[str] = (), solver_identities: Iterable[str] = (), source_root=None,
) -> dict:
    """Reverify local inputs and replay the gate before producing a protocol freeze."""
    manifest = _check_input_manifest(manifest)
    verify_input_manifest(manifest, spec, "dev", memory, registry, extra_files, source_root=source_root)
    gate = evaluate_development_gate(
        spec, records, manifest=manifest, memory=memory, source_resolver=source_resolver,
        independent_reviewers=independent_reviewers, solver_identities=solver_identities,
    )
    if not gate["passed"]:
        raise ResearchControlError("Development gate failed: " + "; ".join(gate["failures"]))
    return _seal(
        "research-protocol-freeze", protocol=manifest["protocol"], protocol_sha256=manifest["protocol_sha256"],
        development_manifest_sha256=manifest["sha256"], development_gate=gate,
    )


def freeze_evaluation(
    spec, *, manifest, protocol_freeze, memory=None, registry=None, extra_files=(), source_root=None,
) -> dict:
    """Bind a new held-out test to the already approved, unchanged protocol."""
    manifest = _check_input_manifest(manifest)
    protocol_freeze = _check_protocol_freeze(protocol_freeze)
    verify_input_manifest(manifest, spec, "test", memory, registry, extra_files, source_root=source_root)
    if manifest["protocol_sha256"] != protocol_freeze["protocol_sha256"]:
        raise ResearchControlError("Held-out evaluation differs from the development-approved protocol")
    result = _seal(
        "research-evaluation-freeze", split="test", input_manifest_sha256=manifest["sha256"],
        protocol_freeze_sha256=protocol_freeze["sha256"], protocol_sha256=manifest["protocol_sha256"],
        split_registry_sha256=manifest["split_registry_sha256"],
        task_manifest_sha256=manifest["active"]["task_manifest_sha256"],
        capture_manifest_sha256=manifest["active"]["capture_manifest_sha256"], schedule_sha256=manifest["active"]["schedule_sha256"],
    )
    if _spec(spec).get("phase_manifest_sha256") not in (None, result["sha256"]):
        raise ResearchControlError("The spec points to a different held-out evaluation freeze")
    return result


def verify_freeze(freeze, manifest, *, protocol_freeze=None, records: Iterable = (), spec=None) -> str:
    """Check freeze/phase/task/protocol identities, including partial resume records.

    Source-byte revalidation is ``verify_input_manifest``'s separate job. Dev
    records bind the candidate SHA; test records bind the evaluation-freeze SHA.
    """
    manifest = _check_input_manifest(manifest)
    rows = _record_rows(records)
    if _plain(freeze).get("kind") == "research-protocol-freeze":
        freeze = _check_protocol_freeze(freeze)
        if manifest["split"] != "dev" or freeze["development_manifest_sha256"] != manifest["sha256"] or freeze["protocol"] != manifest["protocol"]:
            raise ResearchControlError("The protocol freeze belongs to another development candidate")
        phase = manifest["sha256"]
        if rows and _records_digest(rows) != freeze["development_gate"]["records_sha256"]:
            raise ResearchControlError("The frozen development review/records were changed")
    else:
        freeze = _check_seal(freeze, "research-evaluation-freeze")
        approved = _check_protocol_freeze(protocol_freeze)
        bindings = {
            "input_manifest_sha256": manifest["sha256"], "protocol_freeze_sha256": approved["sha256"],
            "protocol_sha256": manifest["protocol_sha256"], "split_registry_sha256": manifest["split_registry_sha256"],
            **{field: manifest["active"][field] for field in ("task_manifest_sha256", "capture_manifest_sha256", "schedule_sha256")},
        }
        if manifest["split"] != "test" or approved["protocol_sha256"] != manifest["protocol_sha256"] or any(freeze.get(field) != value for field, value in bindings.items()):
            raise ResearchControlError("Held-out freeze identities do not match")
        phase = freeze["sha256"]
    if spec is not None:
        bound_spec = _spec(spec)
        _check_spec_binding(bound_spec, manifest)
        if bound_spec.get("phase_manifest_sha256") not in (None, phase):
            raise ResearchControlError("The spec points to another phase/freeze")
    issues = _identity_issues(rows, manifest, phase)
    if issues:
        raise ResearchControlError("Frozen record identity mismatch: " + "; ".join(issues))
    return freeze["sha256"]


def create_review_packages(
    records: Iterable, original_bytes: bytes, *, task_context: Mapping[str, str] | None = None,
    manifest=None, memory=None, source_resolver: Callable[[str], bytes] | None = None,
) -> tuple[dict, dict, dict]:
    """Return ``(public_hunks, evidence_only, private_mapping)`` with disjoint IDs.

    Give only the first package to the hunk reviewer until that review is locked.
    Tokens and order are independently randomized, never derived from run IDs.
    Raw diffs are preserved; semantic clues in a patch cannot be anonymized away.
    A manifest and source reader optionally add replayed excerpts to evidence.
    """
    rows = _record_rows(records)
    try:
        originals = [json.loads(line) for line in original_bytes.decode("utf-8").splitlines() if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise ResearchControlError("Supply the original JSONL result bytes") from exc
    if _records_digest(rows) != _records_digest(_record_rows(originals)):
        raise ResearchControlError("Review records differ from the original results bytes")
    if len({row["id"] for row in rows}) != len(rows):
        raise ResearchControlError("Duplicate review run IDs")
    corpus, reader = None, None
    if manifest is not None:
        corpus = _check_input_manifest(manifest)["protocol"]["source_corpus"]
        reader = _source_reader(memory, source_resolver)
        if corpus is None or (memory is not None and content_sha256(_memory_manifest(memory)) != corpus["corpus_sha256"]):
            raise ResearchControlError("Review replay needs the candidate's frozen source corpus")
    elif memory is not None or source_resolver is not None:
        raise ResearchControlError("Supply the candidate manifest for evidence review replay")
    hunk_items, evidence_items, hunk_mapping, evidence_mapping = [], [], {}, {}
    for run in rows:
        seen_hunks, seen_events = set(), set()
        for hunk in run.get("hunks", []):
            if hunk["id"] in seen_hunks:
                raise ResearchControlError("Duplicate review hunk ID")
            seen_hunks.add(hunk["id"])
            token = "h-" + secrets.token_hex(16)
            hunk_items.append({
                "item_id": token, "path": hunk["path"], "diff": hunk["diff"],
                "added_lines": hunk["added_lines"], "deleted_lines": hunk["deleted_lines"],
                "requirements": (task_context or {}).get(run["task_id"], ""),
                "classification": "unreviewed", "reviewer": None,
            })
            hunk_mapping[token] = {"run_id": run["id"], "hunk_id": hunk["id"]}
        for event in run.get("evidence_decisions", []):
            if event["id"] in seen_events:
                raise ResearchControlError("Duplicate review evidence burst ID")
            seen_events.add(event["id"])
            token = "e-" + secrets.token_hex(16)
            reads = []
            for read in run.get("evidence_reads", []):
                if read["id"] not in event.get("read_ids", []):
                    continue
                item = {key: copy.deepcopy(value) for key, value in read.items() if key not in {"replay", "replay_error"}}
                if reader is not None:
                    try:
                        item["replay"] = _replay_read(read, corpus, reader, include_excerpt=True)
                    except (ResearchControlError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                        item["replay_error"] = str(exc)
                reads.append(item)
            evidence_items.append({
                "item_id": token, "question": event.get("question"), "trigger": event.get("trigger"),
                "before": event.get("before"), "after": event.get("after"), "citations": event.get("citations", []),
                "read_ids": event.get("read_ids", []), "before_action_id": event.get("before_action_id"), "after_action_id": event.get("after_action_id"),
                "reads": reads,
                "actions": run.get("actions", []), "hunks": run.get("hunks", []), "verifications": run.get("verifications", []),
                "material_effect": "unreviewed", "adoption": "unreviewed",
                "adoption_action_ids": [], "contaminated": None, "mechanism_reviewer": None,
            })
            evidence_mapping[token] = {"run_id": run["id"], "event_id": event["id"]}
    secrets.SystemRandom().shuffle(hunk_items)
    secrets.SystemRandom().shuffle(evidence_items)
    hunks = _seal(
        "research-hunk-review", instructions="Review only the task requirements and patch. Lock this review before opening evidence packages or the private mapping. Leave uncertain changes uncertain.", items=hunk_items,
    )
    evidence = _seal(
        "research-evidence-review", instructions="Independently review materiality, freshness, and observable adoption. Replay exact source excerpts with the frozen corpus. This package is not for blinded hunk review.", items=evidence_items,
    )
    private = _seal(
        "research-review-mapping", source_results_sha256=_byte_hash(original_bytes), records_sha256=_records_digest(rows),
        hunk_package_sha256=hunks["sha256"], evidence_package_sha256=evidence["sha256"],
        hunks=hunk_mapping, evidence=evidence_mapping,
    )
    return copy.deepcopy((hunks, evidence, private))
