import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_older_revision_finishes_late():
    request = json.loads("{\"completed_jobs\": [{\"arch\": \"arm64\", \"revision\": \"b4\", \"digest\": \"A\", \"platform\": \"linux\"}, {\"arch\": \"arm64\", \"revision\": \"b3\", \"digest\": \"C\", \"platform\": \"linux\"}, {\"arch\": \"x64\", \"revision\": \"b4\", \"digest\": \"B\", \"platform\": \"linux\"}], \"deployment\": {\"arch\": \"arm64\", \"revision\": \"b4\", \"platform\": \"linux\"}}")
    expected = json.loads("{\"digest\": \"A\"}")
    assert run(request, ROOT) == expected


def test_windows_same_arch():
    request = json.loads("{\"completed_jobs\": [{\"arch\": \"arm64\", \"revision\": \"b4\", \"digest\": \"D\", \"platform\": \"windows\"}, {\"arch\": \"x64\", \"revision\": \"b4\", \"digest\": \"B\", \"platform\": \"linux\"}, {\"arch\": \"arm64\", \"revision\": \"b4\", \"digest\": \"A\", \"platform\": \"linux\"}], \"deployment\": {\"arch\": \"arm64\", \"revision\": \"b4\", \"platform\": \"windows\"}}")
    expected = json.loads("{\"digest\": \"D\"}")
    assert run(request, ROOT) == expected


def test_requested_revision_unpublished():
    request = json.loads("{\"completed_jobs\": [{\"arch\": \"arm64\", \"revision\": \"b4\", \"digest\": \"A\", \"platform\": \"linux\"}, {\"arch\": \"x64\", \"revision\": \"b4\", \"digest\": \"B\", \"platform\": \"linux\"}], \"deployment\": {\"arch\": \"arm64\", \"revision\": \"b3\", \"platform\": \"linux\"}}")
    with pytest.raises(ValueError, match='requested artifact absent'):
        run(request, ROOT)
