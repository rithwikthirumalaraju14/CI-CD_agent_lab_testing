import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_release_manifest_replay():
    request = json.loads("{\"files\": {\"readme\": \"ok\", \"cfg\": \"v1\"}}")
    expected = json.loads("{\"digest\": \"b345303dbc08ba1cb442e5993f963c8a2a585af659fcc15a1835f3fc07f364de\"}")
    assert run(request, ROOT) == expected


def test_catalog_receipt_unicode():
    request = json.loads("{\"files\": {\"\\u00e9t\\u00e9\": \"\", \"z\": \"\\u6771\\u4eac\"}}")
    expected = json.loads("{\"digest\": \"29d3d740feeb403eea7aba1ed1cb0ad2a7a57e1d7f6d63be48eaf9b28e5decfb\"}")
    assert run(request, ROOT) == expected
