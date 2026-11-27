import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_hosted_wirekit_smoke():
    request = json.loads("{\"runner\": \"hosted\", \"timeout\": 3, \"payload\": [\"blue\"]}")
    expected = json.loads("{\"version\": \"2.2.0\", \"page\": [\"blue\"]}")
    assert run(request, ROOT) == expected


def test_self_hosted_wirekit_client():
    request = json.loads("{\"runner\": \"self-hosted\", \"timeout\": 5, \"payload\": [\"blue\"]}")
    expected = json.loads("{\"version\": \"1.4.0\", \"page\": [\"blue\"]}")
    assert run(request, ROOT) == expected
