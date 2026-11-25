"""Create allowlisted standalone snapshots and disjoint trusted verifier bundles."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from .catalogue import get_case, get_seed
from .common import json_bytes, render_checks
from .model import Case, Edit, MaterializedCase, SCHEMA_VERSION, VerificationContract


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_sha256(files: dict[str, bytes]) -> str:
    """Length-framed hash, independent of root path, host OS, and write time."""
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def is_editable(name: str) -> bool:
    return name in {"pyproject.toml", "ci/constraints.txt"} or (
        name.startswith("src/bench_app/") and name.endswith(".py")
    )


def snapshot_files(case_id: str | Case, *, correct_baseline: bool = False, wrong_repair: str | None = None) -> dict[str, bytes]:
    """Return snapshot bytes only. Counterexample selection is operator-only."""
    case = get_case(case_id) if isinstance(case_id, str) else case_id
    seed = get_seed(case)
    files = dict(seed.files)
    # Always validate the anchors, including for correct baselines.
    for change in case.edits:
        change.apply(files)
    if correct_baseline:
        if wrong_repair:
            raise ValueError("A correct baseline cannot also be a wrong-repair candidate")
        for change in reversed(case.edits):
            change.inverse().apply(files)
    if wrong_repair:
        repair = next((item for item in case.wrong_repairs if item.name == wrong_repair), None)
        if repair is None:
            raise KeyError(f"Unknown counterexample repair for {case.id}: {wrong_repair}")
        for change in repair.edits:
            change.apply(files)
    files["tests/test_application.py"] = render_checks(seed.public)
    files["examples/input.json"] = json_bytes(seed.example)
    files["examples/expected.json"] = json_bytes(seed.example_output)
    return files


def _disjoint(left: Path, right: Path) -> bool:
    return left != right and left not in right.parents and right not in left.parents


def _preflight(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Refusing to overwrite an existing destination: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"Destination parent must already exist: {path.parent}")


def _write_tree(destination: Path, files: dict[str, bytes]) -> None:
    for name, data in sorted(files.items()):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ValueError(f"Invalid snapshot path: {name}")
        path = destination.joinpath(*relative.parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def materialize_case(
    case_id: str | Case,
    destination: str | Path,
    *,
    correct_baseline: bool = False,
    verifier_dir: str | Path | None = None,
    wrong_repair: str | None = None,
) -> MaterializedCase:
    """Materialize a new directory, never a checkout overlay.

    Only ``destination`` goes to a solver branch. Persist the returned contract
    on the operator/trusted side. The optional verifier directory MUST be a
    disjoint tree. No files are copied from the operator checkout or environment.
    """
    destination = Path(destination).absolute()
    verifier = Path(verifier_dir).absolute() if verifier_dir is not None else None
    if verifier is not None and not _disjoint(destination.resolve(), verifier.resolve()):
        raise ValueError("Snapshot and verifier directories must be disjoint, not nested")
    _preflight(destination)
    if verifier is not None:
        _preflight(verifier)
    case = get_case(case_id) if isinstance(case_id, str) else case_id
    seed = get_seed(case)
    files = snapshot_files(case, correct_baseline=correct_baseline, wrong_repair=wrong_repair)
    oracle_files = {
        "tests/test_oracle.py": render_checks(seed.oracle, oracle=True),
        "fixtures/checks.json": json_bytes([asdict(check) for check in seed.oracle]),
    }
    contract = VerificationContract(
        schema_version=SCHEMA_VERSION,
        expected_public_tests=len(seed.public),
        expected_oracle_tests=len(seed.oracle),
        protected_sha256={name: sha256(data) for name, data in sorted(files.items()) if not is_editable(name)},
        snapshot_sha256=tree_sha256(files),
        oracle_sha256={name: sha256(data) for name, data in sorted(oracle_files.items())},
    )
    result = MaterializedCase(case.id, destination, verifier, correct_baseline, contract)
    if verifier is not None:
        oracle_files["manifest.json"] = json_bytes(result.to_dict())
    # Stage new trees next to their destinations; existing work is never removed.
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
    oracle_staging = None
    try:
        _write_tree(staging, files)
        if verifier is not None:
            oracle_staging = Path(tempfile.mkdtemp(prefix=".verifier-", dir=verifier.parent))
            _write_tree(oracle_staging, oracle_files)
        # rename(), unlike replace(), refuses an existing directory on Windows.
        # Check explicitly as well for POSIX's empty-directory rename semantics.
        _preflight(destination)
        staging.rename(destination)
        if verifier is not None:
            _preflight(verifier)
            oracle_staging.rename(verifier)
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if oracle_staging is not None and oracle_staging.exists():
            shutil.rmtree(oracle_staging)


def materialize_baseline(case_id: str, destination: str | Path, *, verifier_dir: str | Path | None = None) -> MaterializedCase:
    return materialize_case(case_id, destination, correct_baseline=True, verifier_dir=verifier_dir)


def reference_edits(case_id: str) -> tuple[Edit, ...]:
    """Exact inverse edits; operator-side only, never emitted into any bundle."""
    return tuple(change.inverse() for change in reversed(get_case(case_id).edits))
