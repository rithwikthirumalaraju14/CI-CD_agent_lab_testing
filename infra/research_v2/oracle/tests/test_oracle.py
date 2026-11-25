import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_option_partial_profile():
    request = json.loads("{\"csv\": \"id|amount\\na|1.234,50\\n\", \"profile\": \"europe\", \"options\": {\"csv\": {\"delimiter\": \"|\"}}}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1234.50\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"1234.50\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_signature_only_at_start():
    request = json.loads("{\"csv\": \"\\ufeffid,amount,description\\na,1,keep\\ufeffhere\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"keep\\ufeffhere\"}], \"total\": \"1.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_ties_are_symmetric():
    request = json.loads("{\"csv\": \"id,amount\\na,-1.005\\nb,1.015\\n\", \"minimum\": -2}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"-1.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}, {\"id\": \"b\", \"amount\": \"1.02\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"0.02\", \"count\": 2}")
    assert run(request, ROOT) == expected


def test_negative_offset():
    request = json.loads("{\"csv\": \"id,amount,timestamp\\na,1,2025-04-30T23:15:00-03:00\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2025-05-01T02:15:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"1.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_zero_not_configured_floor():
    request = json.loads("{\"csv\": \"id,amount\\na,0\\n\", \"profile\": \"strict\", \"minimum\": 0}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"0.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"0.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_csv_escaped_quotes():
    request = json.loads("{\"csv\": \"id,amount,description\\na,1,\\\"a, \\\"\\\"b\\\"\\\"\\nc\\\"\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"a, \\\"b\\\"\\nc\"}], \"total\": \"1.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_large_json_negative():
    request = json.loads("{\"format\": \"jsonl\", \"jsonl\": \"\\n{\\\"id\\\":\\\"a\\\",\\\"amount\\\":-9007199254740993.01}\\n\", \"minimum\": \"-9999999999999999\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"-9007199254740993.01\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"-9007199254740993.01\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_dedup_revision_not_lexical():
    request = json.loads("{\"csv\": \"id,amount,revision\\na,10,10\\na,9,9\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"10.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 10, \"description\": \"\"}], \"total\": \"10.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_equal_revision_stability():
    request = json.loads("{\"csv\": \"id,amount,revision\\na,3,2\\na,8,2\\n\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"3.00\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 2, \"description\": \"\"}], \"total\": \"3.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_watermark_same_instant():
    request = json.loads("{\"csv\": \"id,amount,timestamp\\na,1,2025-01-01T01:00:00+01:00\\n\", \"after\": \"2025-01-01T00:00:00Z\"}")
    expected = json.loads("{\"records\": [], \"total\": \"0.00\", \"count\": 0}")
    assert run(request, ROOT) == expected


def test_watermark_after_instant():
    request = json.loads("{\"csv\": \"id,amount,timestamp\\na,1,2025-01-01T00:00:01Z\\n\", \"after\": \"2025-01-01T00:00:00Z\"}")
    expected = json.loads("{\"records\": [{\"id\": \"a\", \"amount\": \"1.00\", \"timestamp\": \"2025-01-01T00:00:01Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"1.00\", \"count\": 1}")
    assert run(request, ROOT) == expected


def test_partner_rounding():
    request = json.loads("{\"csv\": \"external_id,net\\nx,2.345\\n\", \"options\": {\"schema_file\": \"context/schemas/partner.json\"}}")
    expected = json.loads("{\"records\": [{\"id\": \"x\", \"amount\": \"2.34\", \"timestamp\": \"2025-01-01T00:00:00Z\", \"revision\": 1, \"description\": \"\"}], \"total\": \"2.34\", \"count\": 1}")
    assert run(request, ROOT) == expected
