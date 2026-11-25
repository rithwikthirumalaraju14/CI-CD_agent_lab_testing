import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_empty_feed():
    request = json.loads("{\"csv\": \"id,amount\\n\"}")
    expected = json.loads("{\"records\": [], \"total\": \"0.00\", \"count\": 0}")
    assert run(request, ROOT) == expected


def test_ordinary_feed():
    request = json.loads("{\"csv\": \"id,amount\\na,1.25\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.25\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"1.25\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_explicit_options_win():
    request = json.loads("{\"csv\": \"id;amount\\na;2\\n\", \"environment\": {\"csv\": {\"delimiter\": \",\"}}, \"options\": {\"csv\": {\"delimiter\": \";\"}}}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"2.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"2.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_environment_without_explicit_option():
    request = json.loads("{\"csv\": \"id;amount\\na;3\\n\", \"environment\": {\"csv\": {\"delimiter\": \";\"}}}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"3.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"3.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_signature_header():
    request = json.loads("{\"csv\": \"\\ufeffid,amount\\na,1\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"1.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_bankers_rounding():
    request = json.loads("{\"csv\": \"id,amount\\na,1.005\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"1.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_offset_instant():
    request = json.loads("{\"csv\": \"id,amount,timestamp\\na,1,2025-01-01T01:30:00+02:00\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2024-12-31T23:30:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"1.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_nested_profile():
    request = json.loads("{\"csv\": \"id;amount\\na;1.234,50\\n\", \"profile\": \"europe\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1234.50\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"1234.50\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_zero_is_explicit():
    request = json.loads("{\"csv\": \"id,amount\\na,2\\n\", \"profile\": \"strict\", \"minimum\": 0}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"2.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"2.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_null_inherits():
    request = json.loads("{\"csv\": \"id,amount\\na,2\\n\", \"profile\": \"strict\", \"minimum\": null}")
    expected = json.loads("{\"records\": [], \"total\": \"0.00\", \"count\": 0}")
    assert run(request, ROOT) == expected


def test_quoted_newline():
    request = json.loads("{\"csv\": \"id,amount,description\\na,1,\\\"first\\nsecond\\\"\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"first\\nsecond\"}], \"total\": \"1.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_localized_number():
    request = json.loads("{\"csv\": \"id;amount\\na;2.345,67\\n\", \"options\": {\"csv\": {\"delimiter\": \";\", \"decimal\": \",\", \"thousands\": \".\"}}}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"2345.67\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"2345.67\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_json_decimal_precision():
    request = json.loads("{\"format\": \"jsonl\", \"jsonl\": \"{\\\"id\\\":\\\"a\\\",\\\"amount\\\":9007199254740993.01}\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"9007199254740993.01\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"9007199254740993.01\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_revision_selection():
    request = json.loads("{\"csv\": \"id,amount,revision\\na,1,1\\na,9,3\\na,2,2\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"9.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 3, \"description\": \"\"}], \"total\": \"9.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_exclusive_watermark():
    request = json.loads("{\"csv\": \"id,amount,timestamp\\na,1,2025-01-01T00:00:00Z\\n\", \"after\": \"2025-01-01T00:00:00Z\"}")
    expected = json.loads("{\"records\": [], \"total\": \"0.00\", \"count\": 0}")
    assert run(request, ROOT) == expected


def test_partner_aliases():
    request = json.loads("{\"csv\": \"external_id,net\\np,4\\n\", \"options\": {\"schema_file\": \"context/schemas/partner.json\"}}")
    expected = json.loads("{\"records\": [{\"id\": \"p\", \"amount\": \"4.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"4.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_job_relative_source():
    request = json.loads("{\"source_config\": \"context/jobs/nightly.json\"}")
    expected = json.loads("{\"records\": [{\"id\": \"feed\", \"amount\": \"2.50\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"2.50\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_extensionless_gzip():
    request = json.loads("{\"source_config\": \"context/jobs/compressed.json\"}")
    expected = json.loads("{\"records\": [{\"id\": \"packed\", \"amount\": \"3.25\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"3.25\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_naive_timestamp_rejected():
    request = json.loads("{\"csv\": \"id,amount,timestamp\\na,1,2025-01-01T00:00:00\\n\"}")
    with pytest.raises(ValueError, match='explicit UTC offset'):
        run(request, ROOT)


def test_stable_identifier_order():
    request = json.loads("{\"csv\": \"id,amount\\nz,1\\na,2\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"2.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}, {\"id\": \"z\", \"amount\": \"1.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"3.00\", \"count\": 2}")
    assert run(request, ROOT) == expected


def test_retain_duplicates_when_disabled():
    request = json.loads("{\"csv\": \"id,amount\\na,1\\na,2\\n\", \"options\": {\"rules\": {\"deduplicate\": false}}}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}, {\"id\": \"a\", \"amount\": \"2.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"3.00\", \"count\": 2}")
    assert run(request, ROOT) == expected
