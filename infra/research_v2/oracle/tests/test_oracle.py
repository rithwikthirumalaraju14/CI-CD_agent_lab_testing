import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_reordered_partner_manifest():
    request = json.loads("{\"manifest\": {\"z\": [{\"\\u03b2\": \"\\u6771\\u4eac\", \"a\": \"\\u00fc\"}], \"a\": 2}}")
    expected = json.loads("{\"signature\": \"6fca8a157156e9183e3ee9a8440ccaf13a06a693137d69f028bba6a20c01459b\"}")
    assert run(request, ROOT) == expected


def test_array_order_is_signed():
    request = json.loads("{\"manifest\": {\"items\": [\"b\", \"a\"]}}")
    expected = json.loads("{\"signature\": \"013fc18f10321db7541276a147c99e8086d59ebb77cf34ed55bbbe2b7015995c\"}")
    assert run(request, ROOT) == expected


def test_quoted_text():
    request = json.loads("{\"manifest\": {\"label\": \"a\\\"b\"}}")
    expected = json.loads("{\"signature\": \"1ef3e9269ee2a09b436e2def62ee488662dfaf18eaf168480c1290f704640a15\"}")
    assert run(request, ROOT) == expected
