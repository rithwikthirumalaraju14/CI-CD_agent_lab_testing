"""Append-only evidence captures with byte-level checksums and safe log previews."""

import hashlib
import io
import json
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from buildtrace.models import CollectedRun

MAX_LOG_FILE_BYTES = 5 * 1024 * 1024
MAX_EXPANDED_LOG_BYTES = 50 * 1024 * 1024
MAX_LOG_FILES = 500
_CAPTURE_ID = re.compile(r"[0-9a-f]{32}")
_RENAME_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)


class EvidenceError(RuntimeError):
    """An evidence record is unreadable, unsafe, or differs from its manifest."""


def _safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise EvidenceError("Invalid path in evidence record.")
    destination = root.joinpath(*path.parts).resolve()
    if not destination.is_relative_to(root.resolve()) or destination == root.resolve():
        raise EvidenceError("Evidence path points outside its capture.")
    return destination


def _publish_capture(staging: Path, destination: Path) -> None:
    """Retry only the atomic publish, with the same bytes and capture identity.

    Windows scanners can briefly hold a newly written directory open. Rebuilding
    the capture or using replace() here would weaken the append-only contract.
    """
    for attempt in range(len(_RENAME_RETRY_DELAYS) + 1):
        if destination.exists():
            raise FileExistsError("An evidence capture with this ID already exists")
        try:
            staging.rename(destination)
            return
        except PermissionError:
            if attempt == len(_RENAME_RETRY_DELAYS) or not staging.is_dir() or destination.exists():
                raise
            time.sleep(_RENAME_RETRY_DELAYS[attempt])


class EvidenceStore:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory).resolve() / "captures"

    def capture_path(self, capture_id: str) -> Path:
        if not _CAPTURE_ID.fullmatch(capture_id):
            raise FileNotFoundError("Unknown evidence capture")
        return self.directory / capture_id

    def save(self, capture: CollectedRun) -> dict:
        capture_id = uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=True)
        staging = self.directory / f".pending-{capture_id}"
        staging.mkdir()
        manifest = {
            "schema_version": 1,
            "id": capture_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "reference": asdict(capture.reference),
            "run_url": capture.reference.html_url,
            "source_ref": capture.source_ref,
            "run": capture.run,
            "jobs": capture.jobs,
            "artifacts": capture.artifacts,
            "warnings": capture.warnings.copy(),
            "entries": [],
        }

        def write_entry(kind: str, label: str, relative: str, content: bytes) -> None:
            destination = _safe_path(staging, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            manifest["entries"].append(
                {
                    "id": f"entry-{len(manifest['entries']) + 1:04d}",
                    "kind": kind,
                    "label": label,
                    "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "byte_count": len(content),
                }
            )

        try:
            for number, document in enumerate(capture.api_documents, 1):
                write_entry("api", document.endpoint, f"raw/api-{number:04d}.json", document.content)
            for source in capture.sources:
                _safe_path(staging / "repository", source.path)
                write_entry("source", source.path, f"repository/{source.path}", source.content)
            for attachment in capture.attachments:
                _safe_path(staging / "artifacts", attachment.path)
                write_entry("artifact", attachment.path, f"artifacts/{attachment.path}", attachment.content)
            if capture.log_archive is not None:
                write_entry("archive", "Original attempt log archive", "raw/logs.zip", capture.log_archive)
                try:
                    with zipfile.ZipFile(io.BytesIO(capture.log_archive)) as archive:
                        files = [info for info in archive.infolist() if not info.is_dir()]
                        if len(files) > MAX_LOG_FILES:
                            raise EvidenceError("Log previews exceed the 500-file limit.")
                        expanded = 0
                        for number, info in enumerate(sorted(files, key=lambda item: item.filename), 1):
                            if info.file_size > MAX_LOG_FILE_BYTES:
                                manifest["warnings"].append(f"Preview omitted for oversized log: {info.filename}")
                                continue
                            with archive.open(info) as stream:
                                content = stream.read(MAX_LOG_FILE_BYTES + 1)
                            expanded += len(content)
                            if len(content) > MAX_LOG_FILE_BYTES or expanded > MAX_EXPANDED_LOG_BYTES:
                                raise EvidenceError("Expanded logs exceed the viewer's preview limits.")
                            # Archive names are labels only; never extract them as local paths.
                            write_entry("log", info.filename, f"logs/log-{number:04d}.txt", content)
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EvidenceError) as exc:
                    manifest["warnings"].append(f"Some log previews are unavailable: {exc}")
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            _publish_capture(staging, self.capture_path(capture_id))
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return manifest

    def load(self, capture_id: str) -> dict:
        path = self.capture_path(capture_id) / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise EvidenceError("The evidence manifest could not be read.") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("id") != capture_id:
            raise EvidenceError("The evidence manifest has an unsupported format.")
        return manifest

    def read_entry(self, capture_id: str, entry_id: str) -> tuple[dict, bytes]:
        manifest = self.load(capture_id)
        entry = next((item for item in manifest["entries"] if item["id"] == entry_id), None)
        if entry is None:
            raise FileNotFoundError("Unknown evidence entry")
        try:
            content = _safe_path(self.capture_path(capture_id), entry["path"]).read_bytes()
        except FileNotFoundError as exc:
            raise EvidenceError(f"Evidence file is missing: {entry_id}") from exc
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise EvidenceError(f"Evidence checksum mismatch: {entry_id}")
        return entry, content

    def verify(self, capture_id: str) -> dict:
        manifest = self.load(capture_id)
        for entry in manifest["entries"]:
            self.read_entry(capture_id, entry["id"])
        return {"id": capture_id, "integrity": "verified", "files_checked": len(manifest["entries"])}

    def recent(self, limit: int = 10) -> list[dict]:
        if not self.directory.exists():
            return []
        captures = []
        for directory in self.directory.iterdir():
            if directory.is_dir() and _CAPTURE_ID.fullmatch(directory.name):
                try:
                    captures.append(self.load(directory.name))
                except (FileNotFoundError, EvidenceError):
                    continue
        return sorted(captures, key=lambda item: item["captured_at"], reverse=True)[:limit]
