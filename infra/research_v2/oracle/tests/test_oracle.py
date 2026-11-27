import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_canonical_spellings_case_distinct():
    request = json.loads("{\"shards\": [{\"region\": \"\\u00c9\", \"mean_ms\": \"6\", \"count\": 2}, {\"region\": \"E\\u0301\", \"mean_ms\": \"0\", \"count\": 1}, {\"region\": \"\\u00e9\", \"mean_ms\": \"7\", \"count\": 1}], \"limit_ms\": \"6\"}")
    expected = json.loads("{\"means_ms\": {\"\\u00c9\": \"4.000\", \"\\u00e9\": \"7.000\"}, \"gate\": \"hold\"}")
    assert run(request, ROOT) == expected


def test_submillisecond_threshold():
    request = json.loads("{\"shards\": [{\"region\": \"r\", \"mean_ms\": \"1.0004\", \"count\": 3}, {\"region\": \"s\", \"mean_ms\": \"80\", \"count\": 0}], \"limit_ms\": \"1\"}")
    expected = json.loads("{\"means_ms\": {\"r\": \"1.000\", \"s\": null}, \"gate\": \"hold\"}")
    assert run(request, ROOT) == expected


def test_threshold_equality():
    request = json.loads("{\"shards\": [{\"region\": \"r\", \"mean_ms\": \"2\", \"count\": 2}, {\"region\": \"r\", \"mean_ms\": \"8\", \"count\": 1}], \"limit_ms\": \"4\"}")
    expected = json.loads("{\"means_ms\": {\"r\": \"4.000\"}, \"gate\": \"promote\"}")
    assert run(request, ROOT) == expected
