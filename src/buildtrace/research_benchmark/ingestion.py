"""Operator-owned ingestion seed, current fixtures and independent examples."""

from __future__ import annotations

import gzip
import io
from decimal import Decimal

from .common import common_files, json_bytes, text
from .model import Check, Seed


APP = r'''
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
    return list(csv.DictReader(io.StringIO(decoded, newline=""), delimiter=config["csv"]["delimiter"]))


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
            if row["id"] not in latest or row["revision"] > latest[row["id"]]["revision"]:
                latest[row["id"]] = row
        records = [latest[key] for key in sorted(latest)]
    total = sum((Decimal(row["amount"]) for row in records), Decimal("0.00"))
    return {"records": records, "total": str(total), "count": len(records)}
'''


def record(identifier: str = "a", amount: str = "1.00", *, stamp: str = "2025-01-01T00:00:00Z", revision: int = 1, description: str = "") -> dict:
    return {"id": identifier, "amount": amount, "timestamp": stamp, "revision": revision, "description": description}


def output(*records: dict) -> dict:
    return {"records": list(records), "total": str(sum((Decimal(row["amount"]) for row in records), Decimal("0.00"))), "count": len(records)}


def compressed_fixture(data: bytes) -> bytes:
    # gzip.compress(mtime=0) can put a platform-specific OS byte in the header
    # on Python 3.11. GzipFile uses OS=255; stored blocks avoid codec heuristics.
    stream = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0, compresslevel=0) as archive:
        archive.write(data)
    return stream.getvalue()


def seed() -> Seed:
    files = common_files("ingestion")
    files["src/bench_app/app.py"] = text(APP)
    files["context/defaults.json"] = json_bytes({
        "csv": {"encoding": "utf-8-sig", "delimiter": ",", "decimal": ".", "thousands": ""},
        "rules": {"minimum": "0", "deduplicate": True},
        "schema_file": "context/schemas/default.json",
    })
    files["context/profiles.json"] = json_bytes({
        "europe": {"csv": {"delimiter": ";", "decimal": ",", "thousands": "."}},
        "strict": {"rules": {"minimum": "10"}},
    })
    aliases = {"identifier": "id", "amount": "amount", "timestamp": "timestamp", "revision": "revision", "description": "description"}
    files["context/schemas/default.json"] = json_bytes(aliases)
    files["context/schemas/partner.json"] = json_bytes(aliases | {"identifier": "external_id", "amount": "net"})
    files["context/jobs/nightly.json"] = json_bytes({"source": "../feeds/items.csv"})
    files["context/jobs/compressed.json"] = json_bytes({"source": "../feeds/compressed"})
    files["context/feeds/items.csv"] = b"id,amount\nfeed,2.50\n"
    files["context/feeds/compressed"] = compressed_fixture(b"id,amount\npacked,3.25\n")
    files["context/deployment.json"] = json_bytes({
        "profile": "default", "precedence": ["defaults", "profile", "environment", "options"],
        "money_rounding": "half_even", "timestamp_output": "UTC", "watermark": "exclusive",
        "schema": "selected by schema_file", "revision_policy": "maximum, stable identifier order",
    })
    files["context/contract.md"] = text('''
        # Current ingestion contract

        The input is CSV (RFC 4180, including quoted multiline cells) or JSONL.
        CSV defaults accept UTF-8 with or without its signature. Decode strictly;
        corrupt bytes must not be silently discarded. JSON numeric tokens must
        retain decimal precision. Blank JSONL lines are ignored.

        Merge defaults, selected profile, environment and explicit options in that
        order, recursively. The Europe profile changes separators, not encoding.
        minimum=null means use configured minimum; minimum=0 is an explicit zero.
        Apply the configured thousands separator before the decimal separator.
        Round each amount to cents using HALF_EVEN, including negative values.
        Timestamp offsets are mandatory; convert instants to UTC (do not relabel
        their wall clocks). The after watermark is exclusive.

        schema_file chooses an alias map, including partner.json. Source paths in
        a job configuration are relative to that configuration's directory, never
        the process working directory. Compressed feeds can be extensionless;
        recognize gzip by its magic bytes. Deduplication keeps the highest integer
        revision per identifier, then sorts identifiers. Equal revisions keep the
        first row. With deduplicate=false preserve input order.
    ''')
    files["context/deployment-notes.md"] = text('''
        # Other supported deployment contexts

        Retail cash exports use HALF_UP in their own application; the ledger
        importer uses HALF_EVEN. Legacy flat-file jobs use file-level settings;
        current nested profiles inherit unspecified defaults. A source system
        that already emits UTC needs no wall-clock conversion, whereas feeds
        carrying explicit offsets do. These describe legitimate environments,
        not recorded debugging sessions or memory lessons.
    ''')
    public = (
        Check("empty_feed", {"csv": "id,amount\n"}, output()),
        Check("ordinary_feed", {"csv": "id,amount\na,1.25\n"}, output(record(amount="1.25"))),
        Check("explicit_options_win", {"csv": "id;amount\na;2\n", "environment": {"csv": {"delimiter": ","}}, "options": {"csv": {"delimiter": ";"}}}, output(record(amount="2.00"))),
        Check("environment_without_explicit_option", {"csv": "id;amount\na;3\n", "environment": {"csv": {"delimiter": ";"}}}, output(record(amount="3.00"))),
        Check("signature_header", {"csv": "\ufeffid,amount\na,1\n"}, output(record())),
        Check("bankers_rounding", {"csv": "id,amount\na,1.005\n"}, output(record(amount="1.00"))),
        Check("offset_instant", {"csv": "id,amount,timestamp\na,1,2025-01-01T01:30:00+02:00\n"}, output(record(stamp="2024-12-31T23:30:00Z"))),
        Check("nested_profile", {"csv": "id;amount\na;1.234,50\n", "profile": "europe"}, output(record(amount="1234.50"))),
        Check("zero_is_explicit", {"csv": "id,amount\na,2\n", "profile": "strict", "minimum": 0}, output(record(amount="2.00"))),
        Check("null_inherits", {"csv": "id,amount\na,2\n", "profile": "strict", "minimum": None}, output()),
        Check("quoted_newline", {"csv": 'id,amount,description\na,1,"first\nsecond"\n'}, output(record(description="first\nsecond"))),
        Check("localized_number", {"csv": "id;amount\na;2.345,67\n", "options": {"csv": {"delimiter": ";", "decimal": ",", "thousands": "."}}}, output(record(amount="2345.67"))),
        Check("json_decimal_precision", {"format": "jsonl", "jsonl": '{"id":"a","amount":9007199254740993.01}\n'}, output(record(amount="9007199254740993.01"))),
        Check("revision_selection", {"csv": "id,amount,revision\na,1,1\na,9,3\na,2,2\n"}, output(record(amount="9.00", revision=3))),
        Check("exclusive_watermark", {"csv": "id,amount,timestamp\na,1,2025-01-01T00:00:00Z\n", "after": "2025-01-01T00:00:00Z"}, output()),
        Check("partner_aliases", {"csv": "external_id,net\np,4\n", "options": {"schema_file": "context/schemas/partner.json"}}, output(record("p", "4.00"))),
        Check("job_relative_source", {"source_config": "context/jobs/nightly.json"}, output(record("feed", "2.50"))),
        Check("extensionless_gzip", {"source_config": "context/jobs/compressed.json"}, output(record("packed", "3.25"))),
        Check("naive_timestamp_rejected", {"csv": "id,amount,timestamp\na,1,2025-01-01T00:00:00\n"}, error="explicit UTC offset"),
        Check("stable_identifier_order", {"csv": "id,amount\nz,1\na,2\n"}, output(record("a", "2.00"), record("z", "1.00"))),
        Check("retain_duplicates_when_disabled", {"csv": "id,amount\na,1\na,2\n", "options": {"rules": {"deduplicate": False}}}, output(record(), record(amount="2.00"))),
    )
    oracle = (
        Check("option_partial_profile", {"csv": "id|amount\na|1.234,50\n", "profile": "europe", "options": {"csv": {"delimiter": "|"}}}, output(record(amount="1234.50"))),
        Check("signature_only_at_start", {"csv": "\ufeffid,amount,description\na,1,keep\ufeffhere\n"}, output(record(description="keep\ufeffhere"))),
        Check("ties_are_symmetric", {"csv": "id,amount\na,-1.005\nb,1.015\n", "minimum": -2}, output(record(amount="-1.00"), record("b", "1.02"))),
        Check("negative_offset", {"csv": "id,amount,timestamp\na,1,2025-04-30T23:15:00-03:00\n"}, output(record(stamp="2025-05-01T02:15:00Z"))),
        Check("zero_not_configured_floor", {"csv": "id,amount\na,0\n", "profile": "strict", "minimum": 0}, output(record(amount="0.00"))),
        Check("csv_escaped_quotes", {"csv": 'id,amount,description\na,1,"a, ""b""\nc"\n'}, output(record(description='a, "b"\nc'))),
        Check("large_json_negative", {"format": "jsonl", "jsonl": '\n{"id":"a","amount":-9007199254740993.01}\n', "minimum": "-9999999999999999"}, output(record(amount="-9007199254740993.01"))),
        Check("dedup_revision_not_lexical", {"csv": "id,amount,revision\na,10,10\na,9,9\n"}, output(record(amount="10.00", revision=10))),
        Check("equal_revision_stability", {"csv": "id,amount,revision\na,3,2\na,8,2\n"}, output(record(amount="3.00", revision=2))),
        Check("watermark_same_instant", {"csv": "id,amount,timestamp\na,1,2025-01-01T01:00:00+01:00\n", "after": "2025-01-01T00:00:00Z"}, output()),
        Check("watermark_after_instant", {"csv": "id,amount,timestamp\na,1,2025-01-01T00:00:01Z\n", "after": "2025-01-01T00:00:00Z"}, output(record(stamp="2025-01-01T00:00:01Z"))),
        Check("partner_rounding", {"csv": "external_id,net\nx,2.345\n", "options": {"schema_file": "context/schemas/partner.json"}}, output(record("x", "2.34"))),
    )
    example = {"source_config": "context/jobs/nightly.json"}
    return Seed(files, public, oracle, example, output(record("feed", "2.50")))
