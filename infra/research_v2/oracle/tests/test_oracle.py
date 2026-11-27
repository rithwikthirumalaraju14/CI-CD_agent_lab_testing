import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_rollback_second_page():
    request = json.loads("{\"runner\": \"self-hosted\", \"timeout\": 2, \"payload\": {\"next\": 0}}")
    expected = json.loads("{\"version\": \"1.4.0\", \"page\": {\"next\": 0}}")
    assert run(request, ROOT) == expected


def test_hosted_empty_page():
    request = json.loads("{\"runner\": \"hosted\", \"timeout\": 7, \"payload\": []}")
    expected = json.loads("{\"version\": \"2.2.0\", \"page\": []}")
    assert run(request, ROOT) == expected


def test_rollback_invalid_timeout():
    request = json.loads("{\"runner\": \"self-hosted\", \"timeout\": 0, \"payload\": []}")
    with pytest.raises(ValueError, match='invalid client configuration'):
        run(request, ROOT)
