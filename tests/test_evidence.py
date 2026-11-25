import hashlib
import json

import pytest

from buildtrace.evidence import EvidenceError, EvidenceStore
from buildtrace.models import SourceFile

from conftest import log_zip


def test_original_bytes_checksums_and_immutable_captures(tmp_path, collected_run):
    store = EvidenceStore(tmp_path)
    first = store.save(collected_run)
    second = store.save(collected_run)
    assert first["id"] != second["id"]
    assert store.load(first["id"]) == first
    assert store.verify(first["id"])["files_checked"] == 4
    assert {entry["kind"] for entry in first["entries"]} == {"source", "api", "log", "archive"}
    for entry in first["entries"]:
        _, content = store.read_entry(first["id"], entry["id"])
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        if entry["kind"] == "api":
            assert content == b'{ "id": 12345 }\n'
        if entry["kind"] == "archive":
            assert content == collected_run.log_archive
    assert len(store.recent()) == 2


def test_archive_member_paths_never_escape_capture(tmp_path, collected_run):
    collected_run.log_archive = log_zip({"../../escape.txt": b"untrusted log name"})
    store = EvidenceStore(tmp_path)
    manifest = store.save(collected_run)
    log = next(entry for entry in manifest["entries"] if entry["kind"] == "log")
    assert log["label"] == "../../escape.txt"
    assert log["path"] == "logs/log-0001.txt"
    assert store.read_entry(manifest["id"], log["id"])[1] == b"untrusted log name"
    assert not (tmp_path / "escape.txt").exists()


def test_corrupt_log_archive_is_retained_with_a_note(tmp_path, collected_run):
    collected_run.log_archive = b"not a zip archive"
    store = EvidenceStore(tmp_path)
    manifest = store.save(collected_run)
    assert any(entry["kind"] == "archive" for entry in manifest["entries"])
    assert not any(entry["kind"] == "log" for entry in manifest["entries"])
    assert any("previews are unavailable" in warning for warning in manifest["warnings"])


def test_oversized_log_preview_does_not_remove_original_archive(tmp_path, collected_run, monkeypatch):
    monkeypatch.setattr("buildtrace.evidence.MAX_LOG_FILE_BYTES", 4)
    store = EvidenceStore(tmp_path)
    manifest = store.save(collected_run)
    assert any(entry["kind"] == "archive" for entry in manifest["entries"])
    assert not any(entry["kind"] == "log" for entry in manifest["entries"])
    assert any("oversized log" in warning for warning in manifest["warnings"])


def test_modified_evidence_fails_integrity_check(tmp_path, collected_run):
    store = EvidenceStore(tmp_path)
    manifest = store.save(collected_run)
    entry = manifest["entries"][0]
    (store.capture_path(manifest["id"]) / entry["path"]).write_bytes(b"modified")
    with pytest.raises(EvidenceError, match="checksum mismatch"):
        store.read_entry(manifest["id"], entry["id"])
    with pytest.raises(EvidenceError, match="checksum mismatch"):
        store.verify(manifest["id"])


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "C:escape.py", "..\\escape.py"])
def test_unsafe_source_paths_are_rejected_atomically(tmp_path, collected_run, path):
    collected_run.sources = [SourceFile(path, b"content")]
    store = EvidenceStore(tmp_path)
    with pytest.raises(EvidenceError, match="path"):
        store.save(collected_run)
    assert list(store.directory.iterdir()) == []


def test_invalid_capture_ids_cannot_read_other_files(tmp_path):
    store = EvidenceStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load("../../outside")


def test_manifest_paths_are_checked_before_reading(tmp_path, collected_run):
    store = EvidenceStore(tmp_path)
    manifest = store.save(collected_run)
    manifest["entries"][0]["path"] = "../outside"
    (store.capture_path(manifest["id"]) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(EvidenceError, match="path"):
        store.read_entry(manifest["id"], manifest["entries"][0]["id"])
