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
