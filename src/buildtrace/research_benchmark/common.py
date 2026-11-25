"""Common, solver-visible project scaffolding; no case information is emitted."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import textwrap
import zipfile

from .model import Check


def text(value: str) -> bytes:
    return (textwrap.dedent(value).strip() + "\n").encode("utf-8")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


# A small auditable PEP 517/660 backend avoids downloading setuptools or wheel.
# It is deliberately identical across all projects and protected by the verifier.
# Package selection is ordinary project metadata in pyproject.toml.
BUILD_BACKEND = r'''
import base64
import csv
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tomllib
import zipfile


def _config():
    return tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))


def _metadata():
    project = _config()["project"]
    lines = ["Metadata-Version: 2.3", "Name: " + project["name"], "Version: " + project["version"],
             "Requires-Python: " + project["requires-python"]]
    lines += ["Requires-Dist: " + item for item in project.get("dependencies", [])]
    for extra, requirements in project.get("optional-dependencies", {}).items():
        lines.append("Provides-Extra: " + extra)
        lines += [f'Requires-Dist: {item}; extra == "{extra}"' for item in requirements]
    return ("\n".join(lines) + "\n\n").encode()


def _dist_info():
    project = _config()["project"]
    return project["name"].replace("-", "_") + "-" + project["version"] + ".dist-info"


def _entries(editable=False):
    info = _dist_info()
    entries = {
        info + "/METADATA": _metadata(),
        info + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: local-project-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if editable:
        entries["bench_app_local.pth"] = (str(Path("src").resolve()) + "\n").encode()
    else:
        patterns = _config()["tool"]["bench"]["package"]["wheel_include"]
        for path in sorted(Path("src/bench_app").rglob("*")):
            if path.is_file() and any(path.match(pattern) for pattern in patterns):
                entries[path.relative_to("src").as_posix()] = path.read_bytes()
    return entries


def _wheel(directory, editable=False):
    entries = _entries(editable)
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(entries.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, len(data)])
    record_name = _dist_info() + "/RECORD"
    writer.writerow([record_name, "", ""])
    entries[record_name] = record.getvalue().encode()
    filename = _dist_info().removesuffix(".dist-info") + "-py3-none-any.whl"
    Path(directory).mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(Path(directory) / filename, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return filename


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _wheel(wheel_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _wheel(wheel_directory, editable=True)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    directory = Path(metadata_directory) / _dist_info()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "METADATA").write_bytes(_metadata())
    (directory / "WHEEL").write_bytes(_entries()[_dist_info() + "/WHEEL"])
    return directory.name


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel


def get_requires_for_build_wheel(config_settings=None):
    return []


get_requires_for_build_sdist = get_requires_for_build_wheel
get_requires_for_build_editable = get_requires_for_build_wheel


def build_sdist(sdist_directory, config_settings=None):
    config = _config()
    stem = config["project"]["name"].replace("-", "_") + "-" + config["project"]["version"]
    patterns = config["tool"]["bench"]["package"]["sdist_include"]
    Path(sdist_directory).mkdir(parents=True, exist_ok=True)
    filename = stem + ".tar.gz"
    # Gzip mtime/name are fixed too; repeated materializations are byte-identical.
    import gzip
    with (Path(sdist_directory) / filename).open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=1704067200) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in sorted(Path(".").rglob("*")):
                    if not path.is_file() or any(part in {".git", "__pycache__", "dist", ".pytest_cache"} for part in path.parts):
                        continue
                    relative = path.as_posix()
                    if not any(path.match(pattern) for pattern in patterns):
                        continue
                    data = path.read_bytes()
                    info = tarfile.TarInfo(stem + "/" + relative)
                    info.size, info.mtime, info.mode = len(data), 1704067200, 0o644
                    archive.addfile(info, io.BytesIO(data))
    return filename
'''


WORKFLOW = '''
name: Offline project checks
on: [push, workflow_dispatch]
jobs:
  checks:
    runs-on: [self-hosted, buildtrace-offline-python311]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      # The coordinator provisions pinned pytest/build in the trusted runner
      # image/tool cache before this offline project workflow is dispatched.
      - name: Install offline project
        run: python -m pip install --no-index --no-build-isolation --find-links vendor/wheels -c ci/constraints.txt -e '.[dev]'
      - name: Run protected tests
        run: python -m pytest -q tests
      - name: Build distributions
        run: python -m build --no-isolation --wheel --sdist
      - name: Check installed distributions
        run: python ci/check_distributions.py
      - name: Run example
        run: python -m bench_app examples/input.json
'''


DISTRIBUTION_CHECK = r'''
"""Public, generic release smoke check; works without access to a verifier."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def smoke(wheel, cwd):
    # Put the wheel ahead of editable-install .pth entries. Missing resources
    # cannot fall back to the checkout because bench_app is a regular package.
    code = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('bench_app',run_name='__main__')"
    command = [sys.executable, "-c", code, str(wheel), str(ROOT / "examples/input.json")]
    result = subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", capture_output=True)
    if result.returncode:
        raise RuntimeError("installed distribution failed:\n" + result.stdout + result.stderr)
    expected = json.loads((ROOT / "examples/expected.json").read_text(encoding="utf-8"))
    if json.loads(result.stdout) != expected:
        raise RuntimeError("installed distribution returned unexpected CLI output")


def main():
    wheels = list((ROOT / "dist").glob("*.whl"))
    sdists = list((ROOT / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("build exactly one wheel and sdist into a clean dist/ first")
    with tempfile.TemporaryDirectory(prefix="release-check-") as directory:
        work = Path(directory)
        smoke(wheels[0], work)
        source = work / "source"
        source.mkdir()
        with tarfile.open(sdists[0]) as archive:
            for member in archive.getmembers():
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or not member.isfile():
                    raise ValueError("invalid source distribution member")
                target = source / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        roots = list(source.iterdir())
        if len(roots) != 1:
            raise ValueError("source distribution needs one root")
        output = work / "rebuilt"
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(output), str(roots[0])], check=True)
        rebuilt = list(output.glob("*.whl"))
        if len(rebuilt) != 1:
            raise ValueError("rebuild needs exactly one wheel")
        smoke(rebuilt[0], work)
    print("direct wheel and sdist-rebuilt wheel CLI checks passed")


if __name__ == "__main__":
    main()
'''


def common_files(domain: str, *, dependency: str | None = None, pin: str | None = None) -> dict[str, bytes]:
    dependencies = json.dumps([dependency] if dependency else [])
    pyproject = f'''
        [build-system]
        requires = []
        build-backend = "_build_backend"
        backend-path = ["ci"]

        [project]
        name = "bench-{domain}"
        version = "1.4.0"
        description = "Offline {domain} application"
        requires-python = ">=3.11"
        dependencies = {dependencies}

        [project.optional-dependencies]
        dev = ["pytest>=8.3,<9", "build>=1.2,<2"]

        [tool.bench.package]
        wheel_include = ["*.py", "*.json"]
        sdist_include = ["*.toml", "ci/*.py", "src/bench_app/*.py", "src/bench_app/data/*.json"]

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        addopts = "-ra"
    '''
    constraints = "pytest==8.3.5\nbuild==1.2.2.post1\n"
    if pin:
        constraints += pin + "\n"
    return {
        "pyproject.toml": text(pyproject),
        "ci/constraints.txt": constraints.encode(),
        "ci/_build_backend.py": text(BUILD_BACKEND),
        "ci/check_distributions.py": text(DISTRIBUTION_CHECK),
        "src/bench_app/__init__.py": b'"""Offline application package."""\n',
        "src/bench_app/__main__.py": text('''
            import json
            import sys
            from pathlib import Path
            from .app import run


            def main():
                path = Path(sys.argv[1]).resolve()
                request = json.loads(path.read_text(encoding="utf-8"))
                result = run(request, path.parent.parent)
                print(json.dumps(result, sort_keys=True, ensure_ascii=False))


            if __name__ == "__main__":
                main()
        '''),
        "src/bench_app/data/policy.json": json_bytes({"title": "Build ledger", "schema": 2}),
        "vendor/wheels/.keep": b"Offline dependency wheels, when needed, are in this directory.\n",
        ".github/workflows/ci.yml": text(WORKFLOW),
        "context/project.md": text(f'''
            # {domain.title()} application

            This is a standalone offline project. No network service or credentials
            are required. Read contract.md and deployment.json for the current
            deployment; legacy deployment notes describe other supported contexts.
            Application entry point: python -m bench_app examples/input.json.
            Tooling is provisioned by the runner (Python 3.11+, pytest 8.3.5,
            build 1.2.2.post1). Use ci/constraints.txt for project installation.
            ci/_build_backend.py is an auditable stdlib-only PEP 517/660 backend.
            Build both wheel and sdist; installed packages must work away from the
            checkout. tests/, context/, examples/, vendor/ and ci tooling are
            protected; src/bench_app/, pyproject.toml and ci/constraints.txt are editable.
        '''),
    }


def render_checks(checks: tuple[Check, ...], *, oracle: bool = False) -> bytes:
    prefix = '''
import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]
'''
    parts = [textwrap.dedent(prefix).strip(), ""]
    for check in checks:
        parts.append(f"def test_{check.name}():")
        parts.append(f"    request = json.loads({json.dumps(json.dumps(check.request, ensure_ascii=True))})")
        if check.error:
            parts.append(f"    with pytest.raises(ValueError, match={check.error!r}):")
            parts.append("        run(request, ROOT)")
        else:
            parts.append(f"    expected = json.loads({json.dumps(json.dumps(check.expected, ensure_ascii=True))})")
            parts.append("    assert run(request, ROOT) == expected")
        parts.extend(["", ""])
    return ("\n".join(parts).rstrip() + "\n").encode()


def fixture_wheel(version: str, source: str) -> bytes:
    """Deterministic local dependency distribution, including inspectable source."""
    info = f"wirekit-{version}.dist-info"
    entries = {
        "wirekit/__init__.py": text(source),
        info + "/METADATA": f"Metadata-Version: 2.3\nName: wirekit\nVersion: {version}\nRequires-Python: >=3.11\n\n".encode(),
        info + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: offline-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    records = []
    for name, data in sorted(entries.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        records.append(f"{name},sha256={digest},{len(data)}")
    records.append(f"{info}/RECORD,,")
    entries[info + "/RECORD"] = ("\n".join(records) + "\n").encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            entry = zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return stream.getvalue()
