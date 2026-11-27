import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_all_permanent():
    request = json.loads("{\"services\": [{\"id\": \"old\", \"prior\": \"rejected\", \"events\": [200]}, {\"id\": \"new\", \"prior\": \"new\", \"events\": [401, 200]}], \"max_attempts\": 2}")
    expected = json.loads("{\"status\": 502, \"accepted\": [], \"failed\": [\"old\", \"new\"], \"calls\": {\"old\": 0, \"new\": 1}}")
    assert run(request, ROOT) == expected


def test_late_after_budget():
    request = json.loads("{\"services\": [{\"id\": \"a\", \"prior\": \"retryable\", \"events\": [503, 503, 200]}, {\"id\": \"b\", \"prior\": \"accepted\", \"events\": [500]}], \"max_attempts\": 2}")
    expected = json.loads("{\"status\": 207, \"accepted\": [\"b\"], \"failed\": [\"a\"], \"calls\": {\"a\": 2, \"b\": 0}}")
    assert run(request, ROOT) == expected


def test_terminal_replay():
    request = json.loads("{\"services\": [{\"id\": \"b\", \"prior\": \"rejected\", \"events\": [200]}, {\"id\": \"a\", \"prior\": \"accepted\", \"events\": [500]}], \"max_attempts\": 2}")
    expected = json.loads("{\"status\": 207, \"accepted\": [\"a\"], \"failed\": [\"b\"], \"calls\": {\"b\": 0, \"a\": 0}}")
    assert run(request, ROOT) == expected


def test_empty_rollout():
    request = json.loads("{\"services\": [], \"max_attempts\": 2}")
    expected = json.loads("{\"status\": 200, \"accepted\": [], \"failed\": [], \"calls\": {}}")
    assert run(request, ROOT) == expected
