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
