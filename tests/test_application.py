import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_canary_latency_report():
    request = json.loads("{\"shards\": [{\"region\": \"Ouest\", \"mean_ms\": \"900\", \"count\": 1}, {\"region\": \"Ouest\", \"mean_ms\": \"100\", \"count\": 9}], \"limit_ms\": \"200\"}")
    expected = json.loads("{\"means_ms\": {\"Ouest\": \"180.000\"}, \"gate\": \"promote\"}")
    assert run(request, ROOT) == expected


def test_empty_region():
    request = json.loads("{\"shards\": [{\"region\": \"Est\", \"mean_ms\": \"41\", \"count\": 0}], \"limit_ms\": \"300\"}")
    expected = json.loads("{\"means_ms\": {\"Est\": null}, \"gate\": \"promote\"}")
    assert run(request, ROOT) == expected
