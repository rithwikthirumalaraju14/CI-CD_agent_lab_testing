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
        return {"html": "<h1>" + html.escape(title, quote=True) + "</h1><p>" + request.get("text", "") + "</p>"}
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
        return {"versions": sorted(request["versions"], key=str)}
    if operation == "digest":
        data = base64.b64decode(request["base64"], validate=True)
        return {"sha256": hashlib.sha256(data).hexdigest()}
    if operation == "tar":
        return inspect_tar(request["base64"])
    raise ValueError("unknown operation")
