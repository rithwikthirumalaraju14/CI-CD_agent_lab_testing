"""Local, offline verification without installing candidates into the host venv.

The coordinator must pin this code and the external verifier manifest. Candidate
code is executed as Python; this utility is a verification harness, not a sandbox.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

from .materialize import _disjoint, is_editable, sha256
from .model import PhaseResult, VerificationContract, VerificationResult


def load_contract(verifier_dir: str | Path) -> VerificationContract:
    manifest = json.loads((Path(verifier_dir) / "manifest.json").read_text(encoding="utf-8"))
    return VerificationContract.from_dict(manifest["contract"])


def _environment(paths: tuple[Path, ...] = (), project: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONOPTIMIZE", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
        environment.pop(name, None)
    environment.update({
        "PYTHONPATH": os.pathsep.join(str(path) for path in paths),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PIP_NO_INPUT": "1",
        "SOURCE_DATE_EPOCH": "1704067200",
    })
    if project is not None:
        environment["BENCH_PROJECT_ROOT"] = str(project)
    else:
        environment.pop("BENCH_PROJECT_ROOT", None)
    return environment


def _run(phase: str, command: tuple[str, ...], cwd: Path, environment: dict[str, str], timeout: int) -> PhaseResult:
    try:
        result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PhaseResult(phase, False, str(exc), command)
    return PhaseResult(phase, result.returncode == 0, result.stdout + result.stderr, command, result.returncode)


def _ignored_runtime_file(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and (
        parts[0] in {".git", "dist", "build", ".pytest_cache"}
        or "__pycache__" in parts
        or any(part.endswith(".egg-info") for part in parts)
    )


def _protected(project: Path, verifier: Path, contract: VerificationContract) -> PhaseResult:
    if not _disjoint(project.resolve(), verifier.resolve()):
        return PhaseResult("protected", False, "Project and verifier trees must be disjoint")
    for root, expected, allow_manifest in (
        (project, contract.protected_sha256, False), (verifier, contract.oracle_sha256, True),
    ):
        for path in root.rglob("*"):
            name = path.relative_to(root).as_posix()
            if _ignored_runtime_file(name):
                continue
            if path.is_symlink():
                return PhaseResult("protected", False, f"Symlink is not a snapshot file: {name}")
            if not path.is_file():
                continue
            if name not in expected and not (allow_manifest and name == "manifest.json") and not (root == project and is_editable(name)):
                # Do not read unexpected files (in particular environment files).
                return PhaseResult("protected", False, f"Unexpected noneditable file: {name}")
        for name, expected_hash in expected.items():
            path = root.joinpath(*PurePosixPath(name).parts)
            if not path.is_file() or sha256(path.read_bytes()) != expected_hash:
                return PhaseResult("protected", False, f"Protected file missing or changed: {name}")
    return PhaseResult("protected", True, "Protected snapshot and separate oracle hashes match")


def _pytest(
    phase: str, project: Path, tests: Path, paths: tuple[Path, ...], expected: int,
    work: Path, python: str, timeout: int, *, installed: bool = False,
) -> PhaseResult:
    report = work / f"{phase}.xml"
    config = work / "pytest.ini"
    config.write_text("[pytest]\naddopts =\n", encoding="utf-8")
    environment = _environment(paths, project)
    proof = ""
    if installed:
        # Explicitly prove that the candidate wheel, not an editable checkout,
        # supplied bench_app before running verifier tests from another tree.
        command = (
            python, "-c",
            "import bench_app,os,sys; origin=os.path.normcase(os.path.abspath(bench_app.__file__)); "
            "wheel=os.path.normcase(os.path.abspath(sys.argv[1])); print(origin); "
            "assert origin.startswith(wheel + os.sep), (origin,wheel)",
            str(paths[0]),
        )
        origin = _run(phase, command, work, environment, timeout)
        if not origin.passed:
            return origin
        proof = "Installed origin: " + origin.detail
    command = (
        python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--import-mode=importlib",
        "-c", str(config), "--confcutdir", str(tests), "--junitxml", str(report), str(tests),
    )
    result = _run(phase, command, work if installed else project, environment, timeout)
    try:
        root = ET.parse(report).getroot()
        suites = root.iter("testsuite")
        totals = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
        for suite in suites:
            for name in totals:
                totals[name] += int(suite.get(name, "0"))
        cases = list(root.iter("testcase"))
        passed = result.passed and len(cases) == expected and totals == {"tests": expected, "failures": 0, "errors": 0, "skipped": 0}
        return PhaseResult(phase, passed, proof + result.detail + f"\nExpected exactly {expected} tests, zero skipped: {totals}, testcase elements={len(cases)}", command, result.returncode, totals["tests"])
    except (OSError, ET.ParseError, ValueError) as exc:
        return PhaseResult(phase, False, proof + result.detail + f"\nInvalid/missing JUnit test-count proof: {exc}", command, result.returncode)


def _resolve(project: Path, contract: VerificationContract, work: Path, python: str, timeout: int) -> tuple[PhaseResult, tuple[Path, ...]]:
    report = work / "resolution.json"
    command = (python, *contract.install_argv, "--dry-run", "--report", str(report))
    result = _run("resolve", command, project, _environment(), timeout)
    if not result.passed:
        return result, ()
    try:
        resolution = json.loads(report.read_text(encoding="utf-8"))
        dependencies = []
        for item in resolution["install"]:
            name = item["metadata"]["name"].lower().replace("_", "-")
            if name == "wirekit":
                path = project / "vendor/wheels" / f"wirekit-{item['metadata']['version']}-py3-none-any.whl"
                if not path.is_file():
                    raise ValueError(f"Resolved dependency is not a protected local wheel: {path.name}")
                dependencies.append(path)
        # Usually wirekit is absent from the host venv. If it is already present,
        # make pip resolve only that requirement in a separate offline dry-run so
        # the source and installed-wheel probes still use the selected local wheel.
        if not dependencies:
            import tomllib
            project_config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
            requirements = project_config["project"].get("dependencies", [])
            if requirements:
                dep_report = work / "dependency-resolution.json"
                dep_command = (
                    python, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--no-deps", "--no-index",
                    "--find-links", str(project / "vendor/wheels"), "-c", str(project / "ci/constraints.txt"),
                    "--report", str(dep_report), *requirements,
                )
                dep_result = _run("resolve", dep_command, project, _environment(), timeout)
                if not dep_result.passed:
                    return dep_result, ()
                for item in json.loads(dep_report.read_text(encoding="utf-8"))["install"]:
                    name = item["metadata"]["name"].lower().replace("_", "-")
                    if name != "wirekit":
                        raise ValueError(f"Unsupported external runtime dependency in offline contract: {name}")
                    dependencies.append(project / "vendor/wheels" / f"wirekit-{item['metadata']['version']}-py3-none-any.whl")
        return result, tuple(dependencies)
    except (OSError, KeyError, ValueError) as exc:
        return PhaseResult("resolve", False, result.detail + "\n" + str(exc), command, result.returncode), ()


def _extract_sdist(archive_path: Path, directory: Path) -> Path:
    directory.mkdir()
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in member.name or not member.isfile():
                raise ValueError(f"Invalid sdist member: {member.name}")
            path = directory.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable sdist member: {member.name}")
            path.write_bytes(stream.read())
    roots = list(directory.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Sdist must have exactly one source root")
    return roots[0]


def verify_snapshot(
    project: str | Path,
    verifier_dir: str | Path,
    *,
    contract: VerificationContract | None = None,
    python: str | Path = sys.executable,
    work_dir: str | Path | None = None,
    timeout: int = 90,
) -> VerificationResult:
    """Fail-fast verification, using preinstalled pytest/build and local wheels.

    pip is run with --dry-run/--no-index. No candidate is installed into the
    shared interpreter. Oracle tests are run against both distribution wheels,
    with a wheel-origin assertion and cwd/PYTHONPATH isolated from source.
    The working directory is temporary and never put inside either input tree.
    """
    project, verifier = Path(project).resolve(), Path(verifier_dir).resolve()
    # Keep the venv launcher path: resolving it follows the symlink to the
    # system interpreter and drops the venv's installed test dependencies.
    python = str(Path(python).absolute())
    contract = contract or load_contract(verifier)
    result = VerificationResult()
    if any(type(count) is not int or count <= 0 for count in (contract.expected_public_tests, contract.expected_oracle_tests)):
        result.phases.append(PhaseResult("protected", False, "Expected test counts must be positive"))
        return result
    protected = _protected(project, verifier, contract)
    result.phases.append(protected)
    if not protected.passed:
        return result
    with tempfile.TemporaryDirectory(prefix="verify-project-", dir=work_dir) as directory:
        work = Path(directory).resolve()
        if not _disjoint(project, work) or not _disjoint(verifier, work):
            raise ValueError("Verification work must be outside project and verifier trees")
        phase, dependencies = _resolve(project, contract, work, python, timeout)
        result.phases.append(phase)
        if not phase.passed:
            return result
        phase = _pytest("public", project, project / "tests", (project / "src", *dependencies), contract.expected_public_tests, work, python, timeout)
        result.phases.append(phase)
        if not phase.passed:
            return result
        output = work / "distributions"
        command = (python, *contract.build_argv, "--outdir", str(output), str(project))
        phase = _run("build", command, project, _environment(), timeout)
        wheels, sdists = list(output.glob("*.whl")), list(output.glob("*.tar.gz"))
        if phase.passed and (len(wheels) != 1 or len(sdists) != 1):
            phase = PhaseResult("build", False, phase.detail + "\nExpected exactly one wheel and sdist", command, phase.returncode)
        result.phases.append(phase)
        if not phase.passed:
            return result
        command = (python, *contract.cli_argv)
        phase = _run("cli", command, project, _environment((project / "src", *dependencies), project), timeout)
        if phase.passed:
            try:
                expected = json.loads((project / "examples/expected.json").read_text(encoding="utf-8"))
                if json.loads(phase.detail) != expected:
                    raise ValueError("CLI output differs from protected example output")
            except (ValueError, OSError) as exc:
                phase = PhaseResult("cli", False, phase.detail + "\n" + str(exc), command, phase.returncode)
        result.phases.append(phase)
        if not phase.passed:
            return result
        phase = _pytest("wheel_oracle", project, verifier / "tests", (wheels[0], *dependencies), contract.expected_oracle_tests, work, python, timeout, installed=True)
        result.phases.append(phase)
        if not phase.passed:
            return result
        try:
            source = _extract_sdist(sdists[0], work / "sdist-source")
        except (OSError, ValueError, tarfile.TarError) as exc:
            result.phases.append(PhaseResult("sdist_rebuild", False, str(exc)))
            return result
        rebuilt_output = work / "rebuilt"
        command = (python, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(rebuilt_output), str(source))
        phase = _run("sdist_rebuild", command, work, _environment(), timeout)
        rebuilt = list(rebuilt_output.glob("*.whl"))
        if phase.passed and len(rebuilt) != 1:
            phase = PhaseResult("sdist_rebuild", False, phase.detail + "\nExpected exactly one rebuilt wheel", command, phase.returncode)
        result.phases.append(phase)
        if not phase.passed:
            return result
        result.phases.append(_pytest("sdist_oracle", project, verifier / "tests", (rebuilt[0], *dependencies), contract.expected_oracle_tests, work, python, timeout, installed=True))
    return result
