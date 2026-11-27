import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_release_policy():
    request = json.loads("{\"fields\": [\"channel\"]}")
    expected = json.loads("{\"channel\": \"stable\"}")
    assert run(request, ROOT) == expected


def test_policy_schema():
    request = json.loads("{\"fields\": [\"schema\"]}")
    expected = json.loads("{\"schema\": 3}")
    assert run(request, ROOT) == expected
