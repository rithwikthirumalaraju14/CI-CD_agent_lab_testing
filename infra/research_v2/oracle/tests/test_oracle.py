import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_installed_rollout_policy():
    request = json.loads("{\"fields\": [\"rollout\", \"channel\"]}")
    expected = json.loads("{\"channel\": \"stable\", \"rollout\": 20}")
    assert run(request, ROOT) == expected


def test_empty_projection():
    request = json.loads("{\"fields\": []}")
    expected = json.loads("{}")
    assert run(request, ROOT) == expected
