import csv
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import gzip
import io
import json


def deep_merge(left, right):
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def configuration(request, root):
    defaults = json.loads((root / "context/defaults.json").read_text(encoding="utf-8"))
    profiles = json.loads((root / "context/profiles.json").read_text(encoding="utf-8"))
    config = deep_merge(defaults, profiles.get(request.get("profile"), {}))
    config = deep_merge(config, request.get("environment", {}))
    config = deep_merge(config, request.get("options", {}))
    return config


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def source_bytes(request, root):
    if "source_config" in request:
        path = root / request["source_config"]
        settings = json.loads(path.read_text(encoding="utf-8"))
        source = path.parent / settings["source"]
        data = source.read_bytes()
    else:
        data = request.get("csv", "").encode(request.get("encoding", "utf-8"))
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    return data


def rows_from(request, root, config):
    if request.get("format") == "jsonl":
        return [json.loads(line, parse_float=Decimal) for line in request["jsonl"].splitlines() if line.strip()]
    decoded = source_bytes(request, root).decode(config["csv"]["encoding"])
    return list(csv.DictReader(decoded.splitlines(), delimiter=config["csv"]["delimiter"]))


def amount(value, config):
    value = str(value).replace(config["csv"]["thousands"], "") if config["csv"]["thousands"] else str(value)
    value = value.replace(config["csv"]["decimal"], ".")
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def normalize(row, aliases, config):
    instant = timestamp(row.get(aliases["timestamp"], "2025-01-01T00:00:00Z"))
    return {
        "id": str(row[aliases["identifier"]]),
        "amount": str(amount(row[aliases["amount"]], config)),
        "timestamp": instant.isoformat().replace("+00:00", "Z"),
        "revision": int(row.get(aliases["revision"], 1)),
        "description": row.get(aliases["description"], ""),
    }


def run(request, root):
    config = configuration(request, root)
    aliases = json.loads((root / config["schema_file"]).read_text(encoding="utf-8"))
    records = [normalize(row, aliases, config) for row in rows_from(request, root, config)]
    minimum = request.get("minimum")
    minimum = minimum if minimum is not None else config["rules"]["minimum"]
    records = [row for row in records if Decimal(row["amount"]) >= Decimal(str(minimum))]
    if request.get("after"):
        watermark = timestamp(request["after"])
        records = [row for row in records if timestamp(row["timestamp"]) > watermark]
    if config["rules"]["deduplicate"]:
        latest = {}
        for row in records:
            if row["id"] not in latest:
                latest[row["id"]] = row
        records = [latest[key] for key in sorted(latest)]
    total = sum((Decimal(row["amount"]) for row in records), Decimal("0.00"))
    return {"records": records, "total": str(total), "count": len(records)}
