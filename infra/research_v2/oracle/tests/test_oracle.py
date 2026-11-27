import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_success_beyond_retry_budget():
    request = json.loads("{\"runner\": \"green\", \"responses\": [503, 503, 503, 200]}")
    expected = json.loads("{\"action\": \"rollback\", \"attempts\": 3}")
    assert run(request, ROOT) == expected


def test_immediate_success():
    request = json.loads("{\"runner\": \"green\", \"responses\": [200, 503]}")
    expected = json.loads("{\"action\": \"promote\", \"attempts\": 1}")
    assert run(request, ROOT) == expected


def test_true_exhaustion():
    request = json.loads("{\"runner\": \"blue\", \"responses\": [503, 503, 503]}")
    expected = json.loads("{\"action\": \"rollback\", \"attempts\": 3}")
    assert run(request, ROOT) == expected


def test_short_probe_log():
    request = json.loads("{\"runner\": \"green\", \"responses\": [503]}")
    expected = json.loads("{\"action\": \"rollback\", \"attempts\": 1}")
    assert run(request, ROOT) == expected
