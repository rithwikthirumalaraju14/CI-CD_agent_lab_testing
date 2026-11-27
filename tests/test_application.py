import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_ascii_release_signature():
    request = json.loads("{\"manifest\": {\"build\": 7}}")
    expected = json.loads("{\"signature\": \"7c02a5bac80b53d7403310592c0a3b8e5dd9eff29bcee8ee844eb3bd9b40e6a5\"}")
    assert run(request, ROOT) == expected


def test_partner_release_signature():
    request = json.loads("{\"manifest\": {\"release\": {\"tag\": \"\\u00e9t\\u00e9\", \"arch\": \"arm64\"}}}")
    expected = json.loads("{\"signature\": \"e17de4879e84124b9196767c3d4d56eb19ae3623b40225e5179b657f22c6d3cf\"}")
    assert run(request, ROOT) == expected
