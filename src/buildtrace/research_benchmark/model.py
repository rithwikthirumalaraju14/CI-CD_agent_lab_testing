"""Operator-side data contracts for the offline research benchmark.

Nothing in this package is a dependency of a generated project.  In particular,
case identities, edits, expected answers, and verifier manifests stay here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "buildtrace.research-benchmark.v2"


class AnchorError(ValueError):
    """An operator edit no longer matches the exact seed it was written for."""


@dataclass(frozen=True)
class Edit:
    path: str
    before: str
    after: str

    def apply(self, files: dict[str, bytes]) -> None:
        if self.path not in files:
            raise AnchorError(f"Missing edit target: {self.path}")
        text = files[self.path].decode("utf-8")
        count = text.count(self.before)
        if not self.before or count != 1:
            raise AnchorError(f"{self.path}: expected exactly one anchor, found {count}: {self.before!r}")
        if self.before == self.after:
            raise AnchorError(f"No-op edit: {self.path}")
        files[self.path] = text.replace(self.before, self.after, 1).encode("utf-8")

    def inverse(self) -> Edit:
        return Edit(self.path, self.after, self.before)


@dataclass(frozen=True)
class WrongRepair:
    """A hypothetical repair candidate, NOT a historical agent observation."""

    name: str
    rationale: str
    edits: tuple[Edit, ...]
    rejection: str = "public"


@dataclass(frozen=True)
class Case:
    id: str
    split: str
    domain: str
    family: str
    title: str
    mechanism: str
    tags: tuple[str, ...]
    edits: tuple[Edit, ...]
    wrong_repairs: tuple[WrongRepair, ...] = ()
    environment: str = "default"
    variant: str | None = None
    steps: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "split": self.split,
            "domain": self.domain,
            "environment": self.environment,
            "family": self.family,
            "variant": self.variant,
            "title": self.title,
            "mechanism": self.mechanism,
            "tags": list(self.tags),
            "mutation_count": len(self.edits),
            "repair_steps": list(self.steps),
            "hypothetical_wrong_repairs": [
                {"name": repair.name, "rationale": repair.rationale, "rejection": repair.rejection}
                for repair in self.wrong_repairs
            ],
        }


@dataclass(frozen=True)
class Check:
    name: str
    request: dict[str, Any]
    expected: Any = None
    error: str | None = None


@dataclass(frozen=True)
class Seed:
    files: dict[str, bytes]
    public: tuple[Check, ...]
    oracle: tuple[Check, ...]
    example: dict[str, Any]
    example_output: Any


@dataclass(frozen=True)
class VerificationContract:
    schema_version: str
    expected_public_tests: int
    expected_oracle_tests: int
    protected_sha256: dict[str, str]
    snapshot_sha256: str
    oracle_sha256: dict[str, str]
    editable_paths: tuple[str, ...] = ("src/bench_app/*.py", "src/bench_app/**/*.py", "pyproject.toml", "ci/constraints.txt")
    protected_paths: tuple[str, ...] = (
        "tests/", "context/", "examples/", "vendor/", "src/bench_app/data/",
        "ci/_build_backend.py", "ci/check_distributions.py", ".github/",
    )
    workflow: str = "ci.yml"
    required_steps: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "checks": ("Install offline project", "Run protected tests", "Build distributions", "Check installed distributions", "Run example"),
    })
    python: str = ">=3.11"
    install_argv: tuple[str, ...] = (
        "-m", "pip", "install", "--no-index", "--no-build-isolation",
        "--find-links", "vendor/wheels", "-c", "ci/constraints.txt", "-e", ".[dev]",
    )
    test_argv: tuple[str, ...] = ("-m", "pytest", "-q", "tests")
    build_argv: tuple[str, ...] = ("-m", "build", "--no-isolation", "--wheel", "--sdist")
    distribution_argv: tuple[str, ...] = ("ci/check_distributions.py",)
    cli_argv: tuple[str, ...] = ("-m", "bench_app", "examples/input.json")
    required_phases: tuple[str, ...] = (
        "protected", "resolve", "public", "build", "cli", "wheel_oracle", "sdist_rebuild", "sdist_oracle",
    )
    require_zero_skips: bool = True
    require_exact_test_counts: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> VerificationContract:
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported verification contract schema")
        fields = dict(value)
        for key in (
            "editable_paths", "protected_paths", "install_argv", "test_argv",
            "build_argv", "distribution_argv", "cli_argv", "required_phases",
        ):
            if key in fields:
                fields[key] = tuple(fields[key])
        if "required_steps" in fields:
            fields["required_steps"] = {job: tuple(steps) for job, steps in fields["required_steps"].items()}
        return cls(**fields)


@dataclass(frozen=True)
class MaterializedCase:
    case_id: str
    destination: Path
    verifier_dir: Path | None
    correct_baseline: bool
    contract: VerificationContract

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "destination": str(self.destination),
            "verifier_dir": str(self.verifier_dir) if self.verifier_dir else None,
            "correct_baseline": self.correct_baseline,
            "contract": self.contract.to_dict(),
        }


@dataclass(frozen=True)
class PhaseResult:
    phase: str
    passed: bool
    detail: str
    command: tuple[str, ...] = ()
    returncode: int | None = None
    tests: int | None = None


@dataclass
class VerificationResult:
    phases: list[PhaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        expected = {"protected", "resolve", "public", "build", "cli", "wheel_oracle", "sdist_rebuild", "sdist_oracle"}
        return {phase.phase for phase in self.phases} == expected and all(phase.passed for phase in self.phases)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "phases": [asdict(phase) for phase in self.phases]}
