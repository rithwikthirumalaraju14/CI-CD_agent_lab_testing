"""Standalone hosted runtime for buildtrace.research-benchmark.v2.

Deploy this generic file as .github/research_v2/verify.py. It needs only the
stdlib plus preprovisioned pip/pytest/build; it never imports the operator agent,
generator, catalogue, or reference edits. Project resolution/builds are offline.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET


SCHEMA = "buildtrace.research-benchmark.v2"
PUBLIC_SCHEMA = "buildtrace.research-benchmark.public.v2"
PHASES = ("protected", "resolve", "public", "build", "cli", "wheel_oracle", "sdist_rebuild", "sdist_oracle")
PUBLIC_PHASES = PHASES[:5]
REQUIRED_STEPS = {"checks": [
    "Resolve offline project", "Run protected tests", "Build distributions",
    "Run example", "Check installed distributions", "Verify protected evidence",
]}
PUBLIC_DESCRIPTOR = ".github/research_v2/public.json"
RUNTIME_PATH = ".github/research_v2/verify.py"
CONTRACT_PATH = "infra/research_v2/contract.json"


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def safe_path(root, name):
    if not isinstance(name, str):
        raise ValueError("Contract paths must be strings")
    relative = PurePosixPath(name)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts or any(char in name for char in ("\\", ":", "\0")):
        raise ValueError("Unsafe contract path")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("Contract paths must stay inside their checkout")
    return path


def editable(name):
    return name in {"pyproject.toml", "ci/constraints.txt"} or name.startswith("src/bench_app/") and name.endswith(".py")


def ignored(name):
    parts = PurePosixPath(name).parts
    return bool(parts) and (parts[0] in {".git", "dist", "build", ".pytest_cache"} or "__pycache__" in parts or any(part.endswith(".egg-info") for part in parts))


def validate_contract(document):
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA:
        raise ValueError("Unsupported hosted verification contract")
    if any(type(document.get(key)) is not int or document[key] < 1 for key in ("expected_public_tests", "expected_oracle_tests")):
        raise ValueError("Separate positive public/oracle counts are required")
    if document.get("required_phases") != list(PHASES) or document.get("require_exact_test_counts") is not True or document.get("require_zero_skips") is not True:
        raise ValueError("The complete exact-count, zero-skip phase contract is required")
    for key in ("protected_sha256", "oracle_sha256"):
        entries = document.get(key)
        if not isinstance(entries, dict) or not entries:
            raise ValueError("Protected snapshot and separate oracle hash maps are required")
        for name, digest in entries.items():
            safe_path(Path.cwd(), name)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid protected content hash")
    for key in ("install_argv", "test_argv", "build_argv", "distribution_argv", "cli_argv"):
        if not isinstance(document.get(key), list) or not document[key] or any(not isinstance(value, str) for value in document[key]):
            raise ValueError("Explicit verification argv are required")
    if not {"tests/test_oracle.py", "fixtures/checks.json"} <= document["oracle_sha256"].keys():
        raise ValueError("The separate generator oracle bundle is incomplete")
    return document


def check_tree(root, expected, *, allow_edits=False):
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if ignored(name):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink is not a protected snapshot file: {name}")
        if path.is_file() and name not in expected and not (allow_edits and editable(name)):
            # Reject unexpected files before opening them, including environment files.
            raise ValueError(f"Unexpected noneditable file: {name}")
    for name, digest in expected.items():
        path = safe_path(root, name)
        if not path.is_file() or sha256(path.read_bytes()) != digest:
            raise ValueError(f"Protected file missing or changed: {name}")


def git_sha(root):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10)
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("A full Git checkout SHA is required")
    return sha


def environment(paths=(), project=None, work=None):
    result = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONOPTIMIZE", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_TRUSTED_HOST"):
        result.pop(name, None)
    result.update({
        "PYTHONPATH": os.pathsep.join(str(path) for path in paths), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INDEX": "1",
        "PIP_NO_INPUT": "1", "SOURCE_DATE_EPOCH": "1704067200",
    })
    result.pop("BENCH_PROJECT_ROOT", None)
    if project is not None:
        result["BENCH_PROJECT_ROOT"] = str(project)
    if work is not None:
        result.update({name: str(work) for name in ("TMP", "TEMP", "TMPDIR")})
    return result


def junit(path, expected):
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Oversized JUnit evidence")
        root = ET.fromstring(path.read_bytes())
        if root.tag not in {"testsuites", "testsuite"}:
            raise ValueError("Unsupported JUnit document")
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        counts = [int(suite.get("tests", "0")) for suite in suites]
        totals = {key: sum(int(suite.get(key, "0")) for suite in suites) for key in ("tests", "failures", "errors", "skipped")}
        cases = root.findall(".//testcase")
        passed = (
            bool(suites) and totals == {"tests": expected, "failures": 0, "errors": 0, "skipped": 0}
            and all(value >= 0 for value in counts) and len(cases) == expected
            and all(len(suite.findall("testcase")) == count for suite, count in zip(suites, counts))
            and not any(case.find(key) is not None for case in cases for key in ("failure", "error", "skipped"))
        )
        return {"passed": passed, **totals}
    except (OSError, ValueError, ET.ParseError):
        return {"passed": False, "tests": 0, "failures": 0, "errors": 1, "skipped": 0}


def extract_sdist(archive_path, destination):
    destination.mkdir()
    total = 0
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        if len(members) > 1000:
            raise ValueError("Too many sdist members")
        seen = set()
        for member in members:
            path = safe_path(destination, member.name)
            if not member.isfile() or member.name in seen or member.size > 2 * 1024 * 1024:
                raise ValueError("Invalid, duplicate, linked, or oversized sdist member")
            seen.add(member.name)
            total += member.size
            if total > 20 * 1024 * 1024:
                raise ValueError("Expanded sdist exceeds its limit")
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream:
                path.write_bytes(stream.read(member.size + 1))
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Sdist must have exactly one source root")
    return roots[0]


class HostedVerifier:
    def __init__(self, candidate, trusted, artifacts, work, *, contract_path=CONTRACT_PATH, timeout=90):
        self.project, self.trusted, self.artifacts, self.work = (Path(path).resolve() for path in (candidate, trusted, artifacts, work))
        roots = (self.project, self.trusted, self.artifacts, self.work)
        if any(left.is_relative_to(right) or right.is_relative_to(left) for number, left in enumerate(roots) for right in roots[number + 1:]):
            raise ValueError("Candidate, trusted checkout, evidence, and work must be disjoint")
        self.contract_path = safe_path(self.trusted, contract_path)
        self.oracle = self.contract_path.parent / "oracle"
        self.timeout = timeout

    def _write(self, name, document):
        self.artifacts.mkdir(parents=True, exist_ok=True)
        (self.artifacts / name).write_bytes(canonical(document))

    def _state(self):
        return json.loads((self.artifacts / "phase-results.json").read_bytes())

    def _contract(self):
        raw = self.contract_path.read_bytes()
        document = validate_contract(json.loads(raw))
        if canonical(document) != raw:
            raise ValueError("The deployed trusted contract must use canonical bytes")
        return document, raw

    def initialize(self, *, oracle_mode, source_sha, workflow_sha, run_id, run_attempt):
        if self.artifacts.exists() or self.work.exists():
            raise FileExistsError("Evidence and verification work directories must be new")
        if type(run_id) is not int or run_id < 1 or type(run_attempt) is not int or run_attempt < 1:
            raise ValueError("Positive run ID/attempt are required")
        if git_sha(self.project) != source_sha or git_sha(self.trusted) != workflow_sha:
            raise ValueError("Checkouts do not match the dispatched source/workflow SHAs")
        self.work.mkdir()
        descriptor = json.loads(safe_path(self.project, PUBLIC_DESCRIPTOR).read_bytes())
        if descriptor.get("schema_version") != PUBLIC_SCHEMA or type(descriptor.get("expected_public_tests")) is not int or descriptor["expected_public_tests"] < 1:
            raise ValueError("Invalid protected public descriptor")
        if oracle_mode:
            contract, raw = self._contract()
            expected, oracle_count = contract["protected_sha256"], contract["expected_oracle_tests"]
            if descriptor["expected_public_tests"] != contract["expected_public_tests"]:
                raise ValueError("Public descriptor and trusted contract counts disagree")
            self._write("verification-contract.json", contract)
            digest = sha256(raw)
        else:
            # Target diagnostics need no hidden contract. Record only public files.
            paths = [path for path in self.project.rglob("*") if not ignored(path.relative_to(self.project).as_posix())]
            for path in paths:
                name = path.relative_to(self.project).as_posix()
                if path.name == ".env" or path.name.startswith(".env.") or path.is_symlink() or path.is_file() and not (editable(name) or name.startswith(("tests/", "context/", "examples/", "vendor/", ".github/", "ci/", "src/bench_app/data/"))):
                    raise ValueError(f"Unexpected public snapshot path: {name}")
            expected = {path.relative_to(self.project).as_posix(): sha256(path.read_bytes()) for path in paths if path.is_file() and not editable(path.relative_to(self.project).as_posix())}
            oracle_count, digest = None, None
        state = {
            "schema_version": 2, "contract_schema": SCHEMA, "oracle_mode": bool(oracle_mode),
            "checked_out_sha": source_sha, "workflow_sha": workflow_sha, "run_id": run_id, "run_attempt": run_attempt,
            "expected_public_tests": descriptor["expected_public_tests"], "expected_oracle_tests": oracle_count,
            "verification_contract_sha256": digest, "protected_sha256": expected, "phases": [],
            "dependencies": [], "distributions": {}, "public_distribution": None,
        }
        self._write("environment-before-install.json", {key: value for key, value in state.items() if key not in {"protected_sha256", "phases", "dependencies", "distributions", "public_distribution"}})
        self._write("phase-results.json", state)
        return self.phase("protected")

    def _protected(self, state):
        check_tree(self.project, state["protected_sha256"], allow_edits=True)
        if state["oracle_mode"]:
            contract, raw = self._contract()
            if sha256(raw) != state["verification_contract_sha256"]:
                raise ValueError("Trusted verification contract changed during execution")
            check_tree(self.oracle, contract["oracle_sha256"])

    def _run(self, name, command, cwd, env):
        command = [str(value) for value in command]
        try:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout)
            output, code = result.stdout + result.stderr, result.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            output, code = str(exc), None
        (self.artifacts / f"{name}.log").write_text(output, encoding="utf-8")
        print(f"[{name}] {' '.join(command)}\n{output}", flush=True)
        return {"phase": name, "passed": code == 0, "returncode": code, "command": command, "detail": output[-6000:]}

    def _resolve(self, state):
        import tomllib

        config = tomllib.loads((self.project / "pyproject.toml").read_text(encoding="utf-8"))
        requirements = config["project"].get("dependencies", [])
        groups = [requirements, config.get("build-system", {}).get("requires", []), *config["project"].get("optional-dependencies", {}).values()]
        for group in groups:
            if not isinstance(group, list) or any(not isinstance(value, str) or any(token in value for token in ("://", "@", "/", "\\")) for value in group):
                raise ValueError("Offline project requirements cannot reference URLs or external paths")
        for line in (self.project / "ci/constraints.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and (line.startswith(("-", "/", ".")) or any(token in line for token in ("://", "@", "/", "\\"))):
                raise ValueError("Offline constraints cannot add indexes, URLs, or external paths")
        report = self.work / "resolution.json"
        argv = (self._contract()[0]["install_argv"] if state["oracle_mode"] else [
            "-m", "pip", "install", "--no-index", "--no-build-isolation", "--find-links", "vendor/wheels", "-c", "ci/constraints.txt", "-e", ".[dev]",
        ])
        if "--no-index" not in argv or "--no-build-isolation" not in argv:
            raise ValueError("The trusted resolve command must remain offline")
        result = self._run("resolve", [sys.executable, *argv, "--dry-run", "--report", report], self.project, environment(work=self.work))
        if not result["passed"]:
            return result
        dependencies = []
        if requirements:
            # Resolve runtime requirements separately even if present in the tool
            # environment. Tests always use the selected protected local wheels.
            dep_report = self.work / "dependency-resolution.json"
            dep_result = self._run("runtime-resolution", [
                sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--no-deps", "--no-index",
                "--find-links", self.project / "vendor/wheels", "-c", self.project / "ci/constraints.txt", "--report", dep_report, *requirements,
            ], self.project, environment(work=self.work))
            if not dep_result["passed"]:
                return dict(dep_result, phase="resolve")
            for item in json.loads(dep_report.read_bytes())["install"]:
                if item["metadata"]["name"].lower().replace("_", "-") != "wirekit":
                    raise ValueError("Only bundled wirekit is a supported runtime dependency")
                wheel = self.project / "vendor/wheels" / f"wirekit-{item['metadata']['version']}-py3-none-any.whl"
                name = wheel.relative_to(self.project).as_posix()
                if name not in state["protected_sha256"] or sha256(wheel.read_bytes()) != state["protected_sha256"][name]:
                    raise ValueError("Resolved runtime is not a protected bundled wheel")
                dependencies.append(str(wheel))
        state["dependencies"] = dependencies
        return result

    def _origin(self, name, wheel, state):
        code = (
            "import bench_app,json,os,sys; origin=os.path.normcase(os.path.abspath(bench_app.__file__)); "
            "wheel=os.path.normcase(os.path.abspath(sys.argv[1])); "
            "assert origin.startswith(wheel + os.sep), (origin,wheel); "
            "print(json.dumps({'module_file':origin,'wheel':wheel,'verified':True}))"
        )
        result = self._run(name + "-origin", [sys.executable, "-c", code, wheel], self.work, environment((wheel, *state["dependencies"]), self.project, self.work))
        if not result["passed"]:
            raise ValueError("Distribution import-origin proof failed: " + result["detail"])
        proof = json.loads(result["detail"])
        proof["wheel_sha256"] = sha256(wheel.read_bytes())
        return proof

    def _pytest(self, name, state):
        installed = name != "public"
        wheel = Path(state["distributions"]["direct" if name == "wheel_oracle" else "rebuilt"]["path"]) if installed else None
        origin = self._origin(name, wheel, state) if installed else None
        tests = self.oracle / "tests" if installed else self.project / "tests"
        expected = state["expected_oracle_tests"] if installed else state["expected_public_tests"]
        report = self.artifacts / {"public": "pytest.xml", "wheel_oracle": "wheel-oracle.xml", "sdist_oracle": "sdist-oracle.xml"}[name]
        if report.exists():
            raise ValueError("Refusing to reuse a previous pytest report")
        config = self.work / "pytest.ini"
        config.write_text("[pytest]\naddopts =\n", encoding="utf-8")
        result = self._run(name, [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--import-mode=importlib", "-c", config,
            "--confcutdir", tests, "-o", "python_files=test_*.py", "-o", "python_functions=test_*", "-o", "python_classes=Test*",
            "--junitxml", report, tests,
        ], self.work if installed else self.project, environment((wheel if installed else self.project / "src", *state["dependencies"]), self.project, self.work))
        counts = junit(report, expected)
        result.update(tests=counts["tests"], junit=report.name, counts=counts)
        result["passed"] = result["passed"] and counts["passed"]
        if origin is not None:
            result["origin"] = origin
        return result

    @staticmethod
    def _distribution(path):
        return {"path": str(path), "filename": path.name, "sha256": sha256(path.read_bytes())}

    def _build(self, state, *, rebuild=False):
        name = "sdist_rebuild" if rebuild else "build"
        output = self.work / ("rebuilt" if rebuild else "direct")
        if output.exists():
            raise ValueError("Distribution output must be fresh")
        if rebuild:
            source = extract_sdist(Path(state["distributions"]["sdist"]["path"]), self.work / "sdist-source")
            argv = ["-m", "build", "--no-isolation", "--wheel", "--outdir", output, source]
        else:
            argv = ["-m", "build", "--no-isolation", "--wheel", "--sdist", "--outdir", output, self.project]
        result = self._run(name, [sys.executable, *argv], self.work if rebuild else self.project, environment(work=self.work))
        wheels, sdists = list(output.glob("*.whl")), list(output.glob("*.tar.gz"))
        if result["passed"] and (len(wheels) != 1 or not rebuild and len(sdists) != 1):
            result.update(passed=False, detail=result["detail"] + "\nExactly one wheel and one original sdist are required")
        if result["passed"]:
            state["distributions"]["rebuilt" if rebuild else "direct"] = self._distribution(wheels[0])
            evidence = self.artifacts / "distributions" / ("rebuilt" if rebuild else "direct")
            evidence.mkdir(parents=True)
            shutil.copyfile(wheels[0], evidence / wheels[0].name)
            if not rebuild:
                state["distributions"]["sdist"] = self._distribution(sdists[0])
                shutil.copyfile(sdists[0], evidence / sdists[0].name)
                # The protected, solver-visible release check expects clean dist/.
                public_dist = self.project / "dist"
                public_dist.mkdir(exist_ok=False)
                for path in (wheels[0], sdists[0]):
                    shutil.copyfile(path, public_dist / path.name)
        return result

    def _cli(self, state):
        result = self._run("cli", [sys.executable, "-m", "bench_app", "examples/input.json"], self.project, environment((self.project / "src", *state["dependencies"]), self.project, self.work))
        if result["passed"]:
            actual = json.loads((self.artifacts / "cli.log").read_text(encoding="utf-8"))
            result["passed"] = actual == json.loads((self.project / "examples/expected.json").read_bytes())
        return result

    def phase(self, name):
        state = self._state()
        if any(phase["phase"] == name for phase in state["phases"]) or name == "distribution_smoke" and state["public_distribution"] is not None:
            raise ValueError("A verification phase cannot be rerun")
        try:
            if name == "protected":
                self._protected(state)
                result = {"phase": name, "passed": True, "returncode": 0, "detail": "Protected project and separate oracle hashes match"}
            elif name == "resolve":
                result = self._resolve(state)
            elif name in {"public", "wheel_oracle", "sdist_oracle"}:
                result = self._pytest(name, state)
            elif name in {"build", "sdist_rebuild"}:
                result = self._build(state, rebuild=name == "sdist_rebuild")
            elif name == "cli":
                result = self._cli(state)
            elif name == "distribution_smoke":
                result = self._run(name, [sys.executable, "ci/check_distributions.py"], self.project, environment(state["dependencies"], self.project, self.work))
            else:
                raise ValueError("Unknown hosted verification phase")
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
            result = {"phase": name, "passed": False, "returncode": None, "detail": str(exc)}
            print(f"[{name}] {exc}", flush=True)
        if name == "distribution_smoke":
            state["public_distribution"] = result
        else:
            state["phases"].append(result)
        self._write("phase-results.json", state)
        return 0 if result["passed"] else 1

    def distributions(self):
        state = self._state()
        smoke_code = self.phase("distribution_smoke")
        if state["oracle_mode"]:
            for name in ("wheel_oracle", "sdist_rebuild", "sdist_oracle"):
                if self.phase(name):
                    return 1
        return smoke_code

    def finalize(self):
        state = self._state()
        unchanged = True
        try:
            self._protected(state)
        except (OSError, ValueError) as exc:
            unchanged = False
            print(f"[protected-final] {exc}", flush=True)
        required = PHASES if state["oracle_mode"] else PUBLIC_PHASES
        by_name = {phase["phase"]: phase for phase in state["phases"]}
        phases = [by_name.get(name, {"phase": name, "passed": False, "returncode": None, "detail": "Not run"}) for name in required]
        reports = {"public": ("pytest.xml", state["expected_public_tests"])}
        if state["oracle_mode"]:
            reports.update({name: (filename, state["expected_oracle_tests"]) for name, filename in (("wheel_oracle", "wheel-oracle.xml"), ("sdist_oracle", "sdist-oracle.xml"))})
        complete = unchanged and len(state["phases"]) == len(required) and set(by_name) == set(required)
        complete = complete and all(phase["passed"] is True and type(phase["returncode"]) is int and phase["returncode"] == 0 for phase in phases)
        complete = complete and all(junit(self.artifacts / filename, count)["passed"] for filename, count in reports.values())
        smoke = state["public_distribution"] or {}
        complete = complete and smoke.get("passed") is True and type(smoke.get("returncode")) is int and smoke["returncode"] == 0
        try:
            for name in (("direct", "sdist", "rebuilt") if state["oracle_mode"] else ("direct", "sdist")):
                record = state["distributions"][name]
                path = Path(record["path"])
                complete = complete and path.name == record["filename"] and sha256(path.read_bytes()) == record["sha256"]
            if state["oracle_mode"]:
                origins = []
                for name, distribution in (("wheel_oracle", "direct"), ("sdist_oracle", "rebuilt")):
                    origin = by_name[name]["origin"]
                    record = state["distributions"][distribution]
                    wheel = os.path.normcase(os.path.abspath(record["path"]))
                    complete = complete and origin.get("verified") is True and origin.get("wheel") == wheel
                    complete = complete and origin.get("wheel_sha256") == record["sha256"] and origin.get("module_file", "").startswith(wheel + os.sep)
                    origins.append(wheel)
                complete = complete and len(set(origins)) == 2
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            complete = False
        source_sha, workflow_sha = git_sha(self.project), git_sha(self.trusted)
        complete = complete and source_sha == state["checked_out_sha"] and workflow_sha == state["workflow_sha"]
        proof = {key: state[key] for key in (
            "schema_version", "contract_schema", "oracle_mode", "run_id", "run_attempt", "expected_public_tests", "expected_oracle_tests", "verification_contract_sha256",
        )}
        proof.update(
            checked_out_sha=source_sha, workflow_sha=workflow_sha, phases=phases,
            protected_tree_sha256=sha256(canonical(state["protected_sha256"])), protected_files_unchanged=unchanged,
            distributions=state["distributions"], public_distribution=state["public_distribution"], verification_complete=bool(complete),
        )
        self._write("verification-proof.json", proof)
        return 0 if complete else 1

    def run(self, **metadata):
        code = self.initialize(**metadata)
        if code == 0:
            for name in ("resolve", "public", "build", "cli"):
                code = self.phase(name)
                if code:
                    break
            if code == 0:
                self.distributions()
        return self.finalize()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "resolve", "public", "build", "cli", "distributions", "finalize", "run"])
    for name in ("candidate", "trusted", "artifacts", "work"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--contract-path", default=CONTRACT_PATH)
    parser.add_argument("--oracle-mode", choices=["true", "false"], default="false")
    parser.add_argument("--source-sha")
    parser.add_argument("--workflow-sha")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    args = parser.parse_args(argv)
    try:
        verifier = HostedVerifier(args.candidate, args.trusted, args.artifacts, args.work, contract_path=args.contract_path)
        if args.command in {"init", "run"}:
            metadata = dict(oracle_mode=args.oracle_mode == "true", source_sha=args.source_sha, workflow_sha=args.workflow_sha, run_id=args.run_id, run_attempt=args.run_attempt)
            return verifier.initialize(**metadata) if args.command == "init" else verifier.run(**metadata)
        if args.command == "distributions":
            return verifier.distributions()
        if args.command == "finalize":
            return verifier.finalize()
        return verifier.phase(args.command)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Hosted verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
