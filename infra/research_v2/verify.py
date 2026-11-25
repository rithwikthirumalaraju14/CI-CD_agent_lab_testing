"""Operator-owned verification helper; run this copy from the pinned trusted checkout.

The candidate package is installed separately. Oracle tests and this helper live
on the trusted revision, which need not be part of the agent's source snapshot.
This is verification isolation, not a sandbox for hostile Python code.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath


def _git_sha(directory: Path) -> str:
    sha = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("A full checkout SHA is required")
    return sha


def _contract(trusted: Path, path: Path) -> tuple[dict, Path]:
    if not path.resolve().is_relative_to(trusted.resolve()) or path.is_symlink():
        raise ValueError("The verification contract must be in the trusted checkout")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("Unsupported trusted verification contract")
    count = document.get("expected_test_count")
    if type(count) is not int or count < 1:
        raise ValueError("Set a positive original expected_test_count in the trusted contract")
    relative = document.get("tests_path")
    if not isinstance(relative, str):
        raise ValueError("Set the trusted tests_path")
    parsed = PurePosixPath(relative)
    if not parsed.parts or parsed.is_absolute() or ".." in parsed.parts or "\\" in relative or ":" in relative:
        raise ValueError("The trusted tests_path must be a relative directory")
    tests = trusted.joinpath(*parsed.parts)
    if not tests.is_dir() or tests.is_symlink() or not tests.resolve().is_relative_to(trusted.resolve()):
        raise ValueError("The protected tests are missing or outside the trusted checkout")
    return document, tests


def _tests_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    files = []
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if any(part in {"__pycache__", ".pytest_cache"} for part in relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("Protected tests may not contain symbolic links")
        if path.is_file():
            files.append(path)
    if not files:
        raise ValueError("The protected test directory is empty")
    for path in sorted(files):
        content = path.read_bytes()
        digest.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
        digest.update(str(len(content)).encode("ascii") + b"\0" + content + b"\0")
    return digest.hexdigest()


def _write(path: Path, document: dict, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, indent=2, allow_nan=False) + "\n")


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Verification metadata must be a JSON object")
    return value


def _junit(path: Path, expected: int) -> tuple[int, bool]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return 0, False
        root = ET.fromstring(path.read_bytes())
        if root.tag not in {"testsuites", "testsuite"}:
            return 0, False
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        counts = [int(suite.attrib.get("tests", "0")) for suite in suites]
        observed = sum(counts)
        cases = root.findall(".//testcase")
        passed = (
            bool(suites) and observed == expected and all(count >= 0 for count in counts)
            and len(cases) == expected
            and all(len(suite.findall("testcase")) == count for suite, count in zip(suites, counts))
            and all(int(suite.attrib.get(key, "0")) == 0 for suite in suites for key in ("failures", "errors", "skipped"))
            and not any(case.find(key) is not None for case in cases for key in ("failure", "error", "skipped"))
        )
        return observed, passed
    except (OSError, ET.ParseError, ValueError):
        return 0, False


def record(args) -> int:
    contract, tests = _contract(args.trusted, args.contract)
    candidate_sha, workflow_sha = _git_sha(args.candidate), _git_sha(args.trusted)
    if candidate_sha != args.source_sha or workflow_sha != args.workflow_sha:
        raise ValueError("The checkouts do not match the dispatched source/workflow SHAs")
    candidate, trusted = args.candidate.resolve(), args.trusted.resolve()
    if candidate.is_relative_to(trusted) or trusted.is_relative_to(candidate):
        raise ValueError("Use separate candidate and trusted checkouts")
    if args.artifacts.resolve().is_relative_to(candidate):
        raise ValueError("Keep verification evidence outside the candidate checkout")
    if args.run_id < 1 or args.run_attempt < 1:
        raise ValueError("Use a positive GitHub run ID and attempt")
    _write(args.artifacts / "environment-before-install.json", {
        "schema_version": 1, "checked_out_sha": candidate_sha, "workflow_sha": workflow_sha,
        "oracle_mode": args.oracle_mode == "true", "expected_test_count": contract["expected_test_count"],
        "protected_tests_sha256": _tests_digest(tests),
        "contract_sha256": hashlib.sha256(args.contract.read_bytes()).hexdigest(),
        "run_id": args.run_id, "run_attempt": args.run_attempt, "python": sys.version,
    }, exclusive=True)
    return 0


def run_tests(args) -> int:
    contract, tests = _contract(args.trusted, args.contract)
    initial = _read(args.artifacts / "environment-before-install.json")
    report = args.artifacts.resolve() / "pytest.xml"
    if report.exists():
        raise ValueError("Refusing to reuse an earlier test report")
    environment = os.environ.copy()
    if initial.get("oracle_mode") is True:
        # Ignore candidate discovery settings, plugins, and conftest.py files.
        for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH", "PYTHONHOME"):
            environment.pop(name, None)
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        command = [
            sys.executable, "-I", "-m", "pytest", str(tests.resolve()),
            "-c", str(Path(__file__).with_name("pytest-trusted.ini").resolve()),
            "--rootdir", str(tests.resolve()), "--confcutdir", str(tests.resolve()),
            "--import-mode=importlib", "-p", "no:cacheprovider", "-o", "addopts=",
            "-o", "python_files=test_*.py", "-o", "python_functions=test_*", "-o", "python_classes=Test*",
            f"--junitxml={report}",
        ]
    else:
        command = [sys.executable, "-m", "pytest", f"--junitxml={report}"]
    result = subprocess.run(command, cwd=args.candidate, env=environment, check=False)
    _write(args.artifacts / "test-execution.json", {"returncode": result.returncode}, exclusive=True)
    _, passed = _junit(report, contract["expected_test_count"])
    if initial.get("oracle_mode") is True:
        unchanged = _tests_digest(tests) == initial.get("protected_tests_sha256")
        return 0 if result.returncode == 0 and passed and unchanged else 1
    return result.returncode


def finalize(args) -> int:
    initial = _read(args.artifacts / "environment-before-install.json")
    contract, tests = _contract(args.trusted, args.contract)
    observed, passed = _junit(args.artifacts / "pytest.xml", contract["expected_test_count"])
    try:
        execution = _read(args.artifacts / "test-execution.json")
    except (OSError, ValueError):
        execution = {}
    tests_digest = _tests_digest(tests)
    unchanged = (
        tests_digest == initial.get("protected_tests_sha256")
        and hashlib.sha256(args.contract.read_bytes()).hexdigest() == initial.get("contract_sha256")
    )
    candidate_sha, workflow_sha = _git_sha(args.candidate), _git_sha(args.trusted)
    complete = (
        passed and execution.get("returncode") == 0 and type(execution.get("returncode")) is int
        and unchanged and candidate_sha == initial.get("checked_out_sha")
        and workflow_sha == initial.get("workflow_sha")
        and contract["expected_test_count"] == initial.get("expected_test_count")
    )
    proof = {key: initial[key] for key in ("schema_version", "oracle_mode", "run_id", "run_attempt")}
    proof.update(
        checked_out_sha=candidate_sha, workflow_sha=workflow_sha,
        expected_test_count=contract["expected_test_count"], observed_test_count=observed,
        protected_tests_sha256=tests_digest, protected_tests_unchanged=unchanged,
        verification_complete=complete,
    )
    _write(args.artifacts / "verification-proof.json", proof)
    return 0 if complete else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "test", "finalize"):
        command = commands.add_parser(name)
        for option in ("candidate", "trusted", "contract", "artifacts"):
            command.add_argument("--" + option, type=Path, required=True)
        if name == "record":
            command.add_argument("--oracle-mode", choices=["true", "false"], required=True)
            command.add_argument("--source-sha", required=True)
            command.add_argument("--workflow-sha", required=True)
            command.add_argument("--run-id", type=int, required=True)
            command.add_argument("--run-attempt", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        return {"record": record, "test": run_tests, "finalize": finalize}[args.command](args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # Exception messages here contain verifier paths/contract diagnostics, not environment dumps.
        print(f"Trusted verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
