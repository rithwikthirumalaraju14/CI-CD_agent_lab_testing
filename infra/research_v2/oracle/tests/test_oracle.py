import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_reordered_files():
    request = json.loads("{\"files\": {\"cfg\": \"v1\", \"readme\": \"ok\"}}")
    expected = json.loads("{\"digest\": \"b345303dbc08ba1cb442e5993f963c8a2a585af659fcc15a1835f3fc07f364de\"}")
    assert run(request, ROOT) == expected


def test_prefix_collision_left():
    request = json.loads("{\"files\": {\"ab\": \"c\"}}")
    expected = json.loads("{\"digest\": \"629c2f14f654f026086f2aefe1855533d0897f49df86a19073be17b237361b09\"}")
    assert run(request, ROOT) == expected


def test_prefix_collision_right():
    request = json.loads("{\"files\": {\"a\": \"bc\"}}")
    expected = json.loads("{\"digest\": \"aa795aa4bbb6117911ef062e271bcb05ccfd58ea439da7d46a44e3a3fcefa790\"}")
    assert run(request, ROOT) == expected


def test_distinct_unicode_names():
    request = json.loads("{\"files\": {\"\\u00e9\": \"x\", \"e\\u0301\": \"X\", \"empty\": \"\"}}")
    expected = json.loads("{\"digest\": \"b94ec42907f11f8d6559599c8943794b6711bb1747894a18649c6b7cbb45e4e9\"}")
    assert run(request, ROOT) == expected


def test_empty_release():
    request = json.loads("{\"files\": {}}")
    expected = json.loads("{\"digest\": \"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}")
    assert run(request, ROOT) == expected
