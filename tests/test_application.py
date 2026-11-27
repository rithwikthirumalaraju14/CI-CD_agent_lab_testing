import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_matrix_artifact_original_order():
    request = json.loads("{\"completed_jobs\": [{\"arch\": \"arm64\", \"revision\": \"b4\", \"digest\": \"A\", \"platform\": \"linux\"}, {\"arch\": \"x64\", \"revision\": \"b4\", \"digest\": \"B\", \"platform\": \"linux\"}], \"deployment\": {\"arch\": \"arm64\", \"revision\": \"b4\", \"platform\": \"linux\"}}")
    expected = json.loads("{\"digest\": \"A\"}")
    assert run(request, ROOT) == expected


def test_matrix_artifact_reversed_order():
    request = json.loads("{\"completed_jobs\": [{\"arch\": \"x64\", \"revision\": \"b4\", \"digest\": \"B\", \"platform\": \"linux\"}, {\"arch\": \"arm64\", \"revision\": \"b4\", \"digest\": \"A\", \"platform\": \"linux\"}], \"deployment\": {\"arch\": \"arm64\", \"revision\": \"b4\", \"platform\": \"linux\"}}")
    expected = json.loads("{\"digest\": \"A\"}")
    assert run(request, ROOT) == expected
