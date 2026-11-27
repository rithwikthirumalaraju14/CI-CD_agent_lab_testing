import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_rollout_batch_receipt():
    request = json.loads("{\"services\": [{\"id\": \"api\", \"prior\": \"new\", \"events\": [200]}, {\"id\": \"jobs\", \"prior\": \"new\", \"events\": [503, 201]}, {\"id\": \"web\", \"prior\": \"new\", \"events\": [422]}], \"max_attempts\": 2}")
    expected = json.loads("{\"status\": 207, \"accepted\": [\"api\", \"jobs\"], \"failed\": [\"web\"], \"calls\": {\"api\": 1, \"jobs\": 2, \"web\": 1}}")
    assert run(request, ROOT) == expected


def test_resume_accepted_service():
    request = json.loads("{\"services\": [{\"id\": \"api\", \"prior\": \"accepted\", \"events\": [201]}, {\"id\": \"jobs\", \"prior\": \"retryable\", \"events\": [204]}], \"max_attempts\": 2}")
    expected = json.loads("{\"status\": 200, \"accepted\": [\"api\", \"jobs\"], \"failed\": [], \"calls\": {\"api\": 0, \"jobs\": 1}}")
    assert run(request, ROOT) == expected
