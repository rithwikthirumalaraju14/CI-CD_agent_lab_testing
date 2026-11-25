"""Artifact/report processor seed. All test archives are generated locally."""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile

from .common import common_files, json_bytes, text
from .model import Check, Seed


APP = r'''
import base64
import csv
from decimal import Decimal
import hashlib
import html
from importlib.resources import files
import io
import json
from pathlib import PurePosixPath
import re
import tarfile
import unicodedata
import zipfile


def policy():
    return json.loads(files("bench_app").joinpath("data/policy.json").read_text(encoding="utf-8"))


def manifest_digest(entries):
    digest = hashlib.sha256()
    for name in sorted(entries):
        digest.update(name.encode("utf-8") + b"\0" + entries[name].encode("utf-8") + b"\0")
    return digest.hexdigest()


def archive_report(request):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
        for name, content in sorted(request["files"].items()):
            entry = zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, content.encode("utf-8"))
    with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as archive:
        return {"members": [{"name": item.filename, "timestamp": list(item.date_time)} for item in archive.infolist()]}


def duration_ms(value):
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value)
    if not match or not any(match.groups()):
        raise ValueError("unsupported ISO duration")
    hours, minutes, seconds = (Decimal(part or "0") for part in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return int(total * 1000)


def version_key(value):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value)
    if not match:
        raise ValueError("invalid semantic version")
    major, minor, patch, prerelease = match.groups()
    suffix = (1, ()) if prerelease is None else (0, tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split(".")))
    return int(major), int(minor), int(patch), suffix


def inspect_tar(encoded):
    data = base64.b64decode(encoded, validate=True)
    seen, members = set(), []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive.getmembers():
            name = member.name
            parts = PurePosixPath(name.replace("\\", "/"))
            invalid_path = parts.is_absolute() or ".." in parts.parts or bool(re.match(r"^[A-Za-z]:", str(parts)))
            if invalid_path:
                raise ValueError("unsafe archive path")
            if not member.isfile():
                raise ValueError("only regular archive files are supported")
            canonical = str(parts)
            if canonical in seen:
                raise ValueError("duplicate archive member")
            seen.add(canonical)
            members.append(canonical)
    return {"members": members}


def run(request, root):
    operation = request.get("operation", "render")
    if operation == "render":
        title = policy()["title"]
        return {"html": "<h1>" + html.escape(title, quote=True) + "</h1><p>" + html.escape(request.get("text", ""), quote=True) + "</p>"}
    if operation == "policy":
        return policy()
    if operation == "manifest":
        return {"sha256": manifest_digest(request["files"])}
    if operation == "archive":
        return archive_report(request)
    if operation == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerows(request["rows"])
        return {"csv": stream.getvalue()}
    if operation == "duration":
        return {"milliseconds": duration_ms(request["value"])}
    if operation == "groups":
        groups = {}
        for row in request["rows"]:
            name = unicodedata.normalize("NFC", row["name"])
            groups[name] = groups.get(name, 0) + row["count"]
        return {"groups": groups}
    if operation == "weighted":
        count = sum(row["count"] for row in request["rows"])
        if not count:
            return {"mean_ms": None}
        total = sum(Decimal(str(row["mean_ms"])) * row["count"] for row in request["rows"])
        return {"mean_ms": str((total / count).quantize(Decimal("0.001")))}
    if operation == "releases":
        return {"versions": sorted(request["versions"], key=version_key)}
    if operation == "digest":
        data = base64.b64decode(request["base64"], validate=True)
        return {"sha256": hashlib.sha256(data).hexdigest()}
    if operation == "tar":
        return inspect_tar(request["base64"])
    raise ValueError("unknown operation")
'''


def tar_fixture(*names: str, symlink: bool = False) -> str:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in names:
            info = tarfile.TarInfo(name)
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "outside"
                archive.addfile(info)
            else:
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
    return base64.b64encode(stream.getvalue()).decode()


def seed() -> Seed:
    files = common_files("artifacts")
    files["src/bench_app/app.py"] = text(APP)
    files["context/deployment.json"] = json_bytes({
        "resource": "bench_app/data/policy.json", "distribution": ["wheel", "sdist"],
        "archive_timestamp": "2024-01-01T00:00:00", "unicode_grouping": "NFC",
    })
    files["context/contract.md"] = text('''
        # Artifact and report contract

        policy.json is an installed package resource, not a working-directory
        file. Both wheel and sdist must carry all runtime resources. A wheel
        rebuilt from the sdist has the same behavior as the directly built wheel.
        The pyproject tool.bench.package include patterns control these manifests.
        Run from any working directory, including without the source checkout.

        Render HTML with text and attribute quote escaping. Manifest SHA-256
        processes filenames in Unicode codepoint order: UTF-8 name, NUL, UTF-8
        content, NUL for each entry. ZIP output sorts members, uses stored files,
        mode 0644, and the fixed timestamp 2024-01-01 00:00:00 regardless of built_at.
        CSV uses RFC 4180 quoting and CRLF record separators, retaining embedded
        newlines. Durations accept nonnegative ISO PT hours/minutes/decimal seconds
        and report integer milliseconds. Empty PT and day notation are rejected.

        Group names after NFC normalization (preserve case). Weighted means use
        counts, not an average of group means; emit three decimal places or null
        when total count is zero. Sort semantic versions numerically, prereleases
        before releases, numeric prerelease identifiers before strings; ignore
        +build metadata for ordering. SHA-256 of a base64 artifact hashes decoded
        bytes verbatim, including non-UTF-8 bytes.

        TAR inspection accepts regular files only. Reject absolute paths, drive
        paths and parent components on both POSIX and Windows; normalize slash
        direction and leading ./ before duplicate detection. Reject duplicate
        canonical paths rather than selecting a first/last winner. These rules
        apply even when inspection runs on a different host OS from extraction.
    ''')
    files["context/deployment-notes.md"] = text('''
        # Packaging and report contexts

        Editable development installs can read resources directly from src/.
        Release consumers install wheels, including wheels built from an sdist.
        A source-only success therefore does not describe release behavior.
        Diagnostic HTML must be escaped; an unrelated trusted-HTML renderer has
        a different input contract. Averages of means are valid only when each
        group has equal weight. Artifact byte hashes and normalized text hashes
        intentionally serve different purposes. These are environment notes,
        not synthetic historical lessons or purported agent experiences.
    ''')
    public = (
        Check("installed_policy", {"operation": "policy"}, {"title": "Build ledger", "schema": 2}),
        Check("ordinary_render", {"text": "ready"}, {"html": "<h1>Build ledger</h1><p>ready</p>"}),
        Check("html_untrusted_text", {"text": '<tag a="b">&'}, {"html": "<h1>Build ledger</h1><p>&lt;tag a=&quot;b&quot;&gt;&amp;</p>"}),
        Check("manifest_sorted", {"operation": "manifest", "files": {"b": "2", "a": "1"}}, {"sha256": hashlib.sha256(b"a\x001\x00b\x002\x00").hexdigest()}),
        Check("zip_fixed_timestamp", {"operation": "archive", "files": {"b.txt": "b", "a.txt": "a"}, "built_at": [2025, 4, 5, 6, 7, 8]}, {"members": [{"name": "a.txt", "timestamp": [2024, 1, 1, 0, 0, 0]}, {"name": "b.txt", "timestamp": [2024, 1, 1, 0, 0, 0]}]}),
        Check("csv_quoting", {"operation": "csv", "rows": [["name", "text"], ["a", 'x,"y"\nz']]}, {"csv": 'name,text\r\na,"x,""y""\nz"\r\n'}),
        Check("iso_duration_units", {"operation": "duration", "value": "PT1H2M3.5S"}, {"milliseconds": 3723500}),
        Check("empty_duration_rejected", {"operation": "duration", "value": "PT"}, error="unsupported ISO duration"),
        Check("unicode_canonical_groups", {"operation": "groups", "rows": [{"name": "café", "count": 1}, {"name": "cafe\u0301", "count": 2}]}, {"groups": {"café": 3}}),
        Check("weighted_mean", {"operation": "weighted", "rows": [{"count": 1, "mean_ms": 10}, {"count": 9, "mean_ms": 20}]}, {"mean_ms": "19.000"}),
        Check("zero_count_mean", {"operation": "weighted", "rows": []}, {"mean_ms": None}),
        Check("numeric_release_order", {"operation": "releases", "versions": ["1.10.0", "1.2.0", "1.2.0-rc.1"]}, {"versions": ["1.2.0-rc.1", "1.2.0", "1.10.0"]}),
        Check("binary_artifact_digest", {"operation": "digest", "base64": base64.b64encode(b"\xff\x00\x80\r\n").decode()}, {"sha256": hashlib.sha256(b"\xff\x00\x80\r\n").hexdigest()}),
        Check("tar_regular_file", {"operation": "tar", "base64": tar_fixture("reports/a.txt")}, {"members": ["reports/a.txt"]}),
        Check("tar_posix_parent_rejected", {"operation": "tar", "base64": tar_fixture("../escape.txt")}, error="unsafe archive path"),
        Check("tar_absolute_rejected", {"operation": "tar", "base64": tar_fixture("/escape.txt")}, error="unsafe archive path"),
        Check("tar_canonical_duplicate", {"operation": "tar", "base64": tar_fixture("a.txt", "./a.txt")}, error="duplicate archive member"),
        Check("tar_link_rejected", {"operation": "tar", "base64": tar_fixture("link", symlink=True)}, error="only regular archive files"),
    )
    oracle = (
        Check("policy_outside_checkout", {"operation": "policy"}, {"title": "Build ledger", "schema": 2}),
        Check("html_single_quotes", {"text": "'雪'"}, {"html": "<h1>Build ledger</h1><p>&#x27;雪&#x27;</p>"}),
        Check("manifest_order_unicode", {"operation": "manifest", "files": {"雪": "é", "z": ""}}, {"sha256": hashlib.sha256("z\0\0雪\0é\0".encode()).hexdigest()}),
        Check("archive_time_not_request", {"operation": "archive", "files": {"x": ""}, "built_at": [2030, 12, 31, 23, 59, 58]}, {"members": [{"name": "x", "timestamp": [2024, 1, 1, 0, 0, 0]}]}),
        Check("csv_empty_and_crlf", {"operation": "csv", "rows": [["", "a\r\nb", "雪"]]}, {"csv": ',"a\r\nb",雪\r\n'}),
        Check("fractional_duration", {"operation": "duration", "value": "PT2M0.125S"}, {"milliseconds": 120125}),
        Check("zero_duration", {"operation": "duration", "value": "PT0S"}, {"milliseconds": 0}),
        Check("duration_days_rejected", {"operation": "duration", "value": "P1D"}, error="unsupported ISO duration"),
        Check("nfc_not_casefold", {"operation": "groups", "rows": [{"name": "A", "count": 1}, {"name": "a", "count": 2}]}, {"groups": {"A": 1, "a": 2}}),
        Check("weights_not_sample_count", {"operation": "weighted", "rows": [{"count": 3, "mean_ms": 1}, {"count": 1, "mean_ms": 2}]}, {"mean_ms": "1.250"}),
        Check("semantic_prerelease", {"operation": "releases", "versions": ["2.0.0", "2.0.0-rc.10", "2.0.0-rc.2", "2.0.0-alpha", "2.0.0-1"]}, {"versions": ["2.0.0-1", "2.0.0-alpha", "2.0.0-rc.2", "2.0.0-rc.10", "2.0.0"]}),
        Check("digest_preserves_newlines", {"operation": "digest", "base64": base64.b64encode(b"a\r\nb\n").decode()}, {"sha256": hashlib.sha256(b"a\r\nb\n").hexdigest()}),
        Check("tar_windows_parent", {"operation": "tar", "base64": tar_fixture("..\\escape.txt")}, error="unsafe archive path"),
        Check("tar_windows_drive", {"operation": "tar", "base64": tar_fixture("C:/escape.txt")}, error="unsafe archive path"),
        Check("tar_duplicate_backslash", {"operation": "tar", "base64": tar_fixture("reports/a.txt", "reports\\a.txt")}, error="duplicate archive member"),
        Check("tar_exact_duplicate", {"operation": "tar", "base64": tar_fixture("x", "x")}, error="duplicate archive member"),
    )
    return Seed(files, public, oracle, {"text": "ready"}, {"html": "<h1>Build ledger</h1><p>ready</p>"})
