import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_promotion_replay():
    request = json.loads("{\"ledger\": [{\"kind\": \"promoted\", \"digest\": \"digest-A\", \"tag\": \"prod\"}, {\"kind\": \"built\", \"digest\": \"digest-B\", \"tag\": \"prod\"}], \"tested_source\": \"source-C\", \"build\": {\"source\": \"source-C\", \"digest\": \"digest-C\"}, \"candidate\": {\"source\": \"source-C\", \"digest\": \"digest-C\", \"tag\": \"prod\"}, \"health\": \"bad\"}")
    expected = json.loads("{\"status\": \"failed\", \"active_digest\": \"digest-A\"}")
    assert run(request, ROOT) == expected


def test_new_healthy_build():
    request = json.loads("{\"ledger\": [{\"kind\": \"promoted\", \"digest\": \"digest-A\", \"tag\": \"prod\"}, {\"kind\": \"built\", \"digest\": \"digest-B\", \"tag\": \"prod\"}], \"tested_source\": \"source-C\", \"build\": {\"source\": \"source-C\", \"digest\": \"digest-C\"}, \"candidate\": {\"source\": \"source-C\", \"digest\": \"digest-C\", \"tag\": \"prod\"}, \"health\": \"ok\"}")
    expected = json.loads("{\"status\": \"promoted\", \"active_digest\": \"digest-C\"}")
    assert run(request, ROOT) == expected
