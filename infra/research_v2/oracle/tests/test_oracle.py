import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_same_source_wrong_artifact():
    request = json.loads("{\"ledger\": [{\"kind\": \"promoted\", \"digest\": \"old\", \"tag\": \"prod\"}, {\"kind\": \"built\", \"digest\": \"prior\", \"tag\": \"prod\"}, {\"kind\": \"promoted\", \"digest\": \"stable\", \"tag\": \"previous\"}, {\"kind\": \"built\", \"digest\": \"shadow\", \"tag\": \"prod\"}], \"tested_source\": \"source-C\", \"build\": {\"source\": \"source-C\", \"digest\": \"digest-C\"}, \"candidate\": {\"source\": \"source-C\", \"digest\": \"rebuilt\", \"tag\": \"prod\"}, \"health\": \"ok\"}")
    expected = json.loads("{\"status\": \"rejected\", \"active_digest\": \"stable\"}")
    assert run(request, ROOT) == expected


def test_untested_build():
    request = json.loads("{\"ledger\": [{\"kind\": \"promoted\", \"digest\": \"old\", \"tag\": \"prod\"}, {\"kind\": \"built\", \"digest\": \"prior\", \"tag\": \"prod\"}, {\"kind\": \"promoted\", \"digest\": \"stable\", \"tag\": \"previous\"}, {\"kind\": \"built\", \"digest\": \"shadow\", \"tag\": \"prod\"}], \"tested_source\": \"other\", \"build\": {\"source\": \"source-C\", \"digest\": \"digest-C\"}, \"candidate\": {\"source\": \"source-C\", \"digest\": \"digest-C\", \"tag\": \"prod\"}, \"health\": \"ok\"}")
    expected = json.loads("{\"status\": \"rejected\", \"active_digest\": \"stable\"}")
    assert run(request, ROOT) == expected


def test_offered_other_source():
    request = json.loads("{\"ledger\": [{\"kind\": \"promoted\", \"digest\": \"old\", \"tag\": \"prod\"}, {\"kind\": \"built\", \"digest\": \"prior\", \"tag\": \"prod\"}, {\"kind\": \"promoted\", \"digest\": \"stable\", \"tag\": \"previous\"}, {\"kind\": \"built\", \"digest\": \"shadow\", \"tag\": \"prod\"}], \"tested_source\": \"source-C\", \"build\": {\"source\": \"source-C\", \"digest\": \"digest-C\"}, \"candidate\": {\"source\": \"elsewhere\", \"digest\": \"digest-C\", \"tag\": \"prod\"}, \"health\": \"ok\"}")
    expected = json.loads("{\"status\": \"rejected\", \"active_digest\": \"stable\"}")
    assert run(request, ROOT) == expected


def test_last_promoted_over_tag():
    request = json.loads("{\"ledger\": [{\"kind\": \"promoted\", \"digest\": \"old\", \"tag\": \"prod\"}, {\"kind\": \"built\", \"digest\": \"prior\", \"tag\": \"prod\"}, {\"kind\": \"promoted\", \"digest\": \"stable\", \"tag\": \"previous\"}, {\"kind\": \"built\", \"digest\": \"shadow\", \"tag\": \"prod\"}], \"tested_source\": \"source-C\", \"build\": {\"source\": \"source-C\", \"digest\": \"digest-C\"}, \"candidate\": {\"source\": \"source-C\", \"digest\": \"digest-C\", \"tag\": \"prod\"}, \"health\": \"bad\"}")
    expected = json.loads("{\"status\": \"failed\", \"active_digest\": \"stable\"}")
    assert run(request, ROOT) == expected
