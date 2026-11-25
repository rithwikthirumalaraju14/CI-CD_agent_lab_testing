import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_cursor_multiple_hops():
    request = json.loads("{\"pages\": {\"\": {\"items\": [], \"next_cursor\": 0}, \"0\": {\"items\": [1], \"next_cursor\": \"end\"}, \"end\": {\"items\": [2], \"next_cursor\": null}}}")
    expected = json.loads("{\"items\": [1, 2], \"count\": 2}")
    assert run(request, ROOT) == expected


def test_quoted_upper_charset():
    request = json.loads("{\"operation\": \"decode\", \"body\": \"{\\\"name\\\":\\\"\\u00fcber\\\"}\", \"body_encoding\": \"iso-8859-1\", \"headers\": {\"content-type\": \"application/json; Charset=\\\"ISO-8859-1\\\"\"}}")
    expected = json.loads("{\"name\": \"\\u00fcber\"}")
    assert run(request, ROOT) == expected


def test_error_not_absence():
    request = json.loads("{\"operation\": \"error\", \"status\": 503, \"body\": \"{\\\"error\\\":\\\"maintenance\\\"}\"}")
    expected = json.loads("{\"status\": 503, \"error\": \"maintenance\"}")
    assert run(request, ROOT) == expected


def test_cache_empty_object():
    request = json.loads("{\"operation\": \"conditional\", \"status\": 304, \"cached\": {}}")
    expected = json.loads("{}")
    assert run(request, ROOT) == expected


def test_signature_unicode():
    request = json.loads("{\"operation\": \"sign\", \"payload\": {\"\\u00e9\": [1, null], \"a\": true}}")
    expected = json.loads("{\"signature\": \"d5ab299604242f98cb112fe2ebb339cc0e286f20d1f817f9f9f4f7ac3feffdee\"}")
    assert run(request, ROOT) == expected


def test_key_insertion_independent():
    request = json.loads("{\"operation\": \"key\", \"payload\": {\"a\": \"\\u96ea\", \"z\": 0}}")
    expected = json.loads("{\"key\": \"c26af2a46d6e4cb7dd8fb5dd48dd0b6886fed0246faf5ccbb884e585c61ccc31\"}")
    assert run(request, ROOT) == expected


def test_key_nested_canonical():
    request = json.loads("{\"operation\": \"key\", \"payload\": {\"b\": {\"z\": 2, \"a\": 1}}}")
    expected = json.loads("{\"key\": \"b974e9bf66d8405a008971d93ccb8a3571b9dc8e24eb14ce09c56ef266073898\"}")
    assert run(request, ROOT) == expected


def test_past_date_clamps():
    request = json.loads("{\"operation\": \"retry_delay\", \"retry_after\": \"Tue, 31 Dec 2024 23:59:00 GMT\", \"now\": \"2025-01-01T00:00:00Z\"}")
    expected = json.loads("{\"seconds\": 0}")
    assert run(request, ROOT) == expected


def test_date_not_constant():
    request = json.loads("{\"operation\": \"retry_delay\", \"retry_after\": \"Wed, 01 Jan 2025 00:02:00 GMT\", \"now\": \"2025-01-01T00:00:15Z\"}")
    expected = json.loads("{\"seconds\": 105}")
    assert run(request, ROOT) == expected


def test_one_attempt_only():
    request = json.loads("{\"operation\": \"retry\", \"statuses\": [503, 200], \"max_attempts\": 1}")
    with pytest.raises(ValueError, match='retry exhausted'):
        run(request, ROOT)


def test_success_on_last_attempt():
    request = json.loads("{\"operation\": \"retry\", \"statuses\": [503, 503, 204]}")
    expected = json.loads("{\"status\": 204, \"attempts\": 3}")
    assert run(request, ROOT) == expected


def test_unicode_stream():
    request = json.loads("{\"operation\": \"stream\", \"body\": \"{\\\"label\\\":\\\"\\u96ea\\\"}\"}")
    expected = json.loads("{\"label\": \"\\u96ea\"}")
    assert run(request, ROOT) == expected


def test_empty_query_list():
    request = json.loads("{\"operation\": \"query\", \"params\": {\"a\": [], \"b\": [\"\", \"&\"]}}")
    expected = json.loads("{\"query\": \"b=&b=%26\"}")
    assert run(request, ROOT) == expected


def test_failed_batch_order():
    request = json.loads("{\"operation\": \"batch\", \"results\": [{\"id\": \"z\", \"status\": 400}, {\"id\": \"a\", \"status\": 503}, {\"id\": \"ok\", \"status\": 202}]}")
    expected = json.loads("{\"status\": 207, \"accepted\": [\"ok\"], \"failed\": [\"z\", \"a\"]}")
    assert run(request, ROOT) == expected
