import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_blue_final_readiness():
    request = json.loads("{\"runner\": \"blue\", \"responses\": [503, 503, 200]}")
    expected = json.loads("{\"action\": \"promote\", \"attempts\": 3}")
    assert run(request, ROOT) == expected


def test_green_final_readiness():
    request = json.loads("{\"runner\": \"green\", \"responses\": [503, 503, 200]}")
    expected = json.loads("{\"action\": \"promote\", \"attempts\": 3}")
    assert run(request, ROOT) == expected
