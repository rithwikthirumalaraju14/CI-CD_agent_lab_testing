import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_empty_sync():
    request = json.loads("{}")
    expected = json.loads("{\"items\": [], \"count\": 0}")
    assert run(request, ROOT) == expected


def test_single_page():
    request = json.loads("{\"pages\": {\"\": {\"items\": [\"a\"], \"next_cursor\": null}}}")
    expected = json.loads("{\"items\": [\"a\"], \"count\": 1}")
    assert run(request, ROOT) == expected


def test_opaque_zero_cursor():
    request = json.loads("{\"pages\": {\"\": {\"items\": [\"a\"], \"next_cursor\": 0}, \"0\": {\"items\": [\"b\"], \"next_cursor\": null}}}")
    expected = json.loads("{\"items\": [\"a\", \"b\"], \"count\": 2}")
    assert run(request, ROOT) == expected


def test_utf8_response():
    request = json.loads("{\"operation\": \"decode\", \"body\": \"{\\\"name\\\":\\\"\\u96ea\\\"}\"}")
    expected = json.loads("{\"name\": \"\\u96ea\"}")
    assert run(request, ROOT) == expected


def test_declared_charset():
    request = json.loads("{\"operation\": \"decode\", \"body\": \"{\\\"name\\\":\\\"caf\\u00e9\\\"}\", \"body_encoding\": \"iso-8859-1\", \"headers\": {\"content-type\": \"application/json; charset=iso-8859-1\"}}")
    expected = json.loads("{\"name\": \"caf\\u00e9\"}")
    assert run(request, ROOT) == expected


def test_error_status_preserved():
    request = json.loads("{\"operation\": \"error\", \"status\": 429, \"body\": \"{\\\"error\\\":\\\"slow down\\\"}\"}")
    expected = json.loads("{\"status\": 429, \"error\": \"slow down\"}")
    assert run(request, ROOT) == expected


def test_cached_not_modified():
    request = json.loads("{\"operation\": \"conditional\", \"status\": 304, \"cached\": {\"items\": [1]}}")
    expected = json.loads("{\"items\": [1]}")
    assert run(request, ROOT) == expected


def test_fresh_replaces_cache():
    request = json.loads("{\"operation\": \"conditional\", \"status\": 200, \"body\": \"{\\\"items\\\":[2]}\", \"cached\": {\"items\": [1]}}")
    expected = json.loads("{\"items\": [2]}")
    assert run(request, ROOT) == expected


def test_application_signature():
    request = json.loads("{\"operation\": \"sign\", \"payload\": {\"b\": 2, \"a\": 1}}")
    expected = json.loads("{\"signature\": \"43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777\"}")
    assert run(request, ROOT) == expected


def test_canonical_idempotency():
    request = json.loads("{\"operation\": \"key\", \"payload\": {\"z\": 0, \"a\": \"\\u96ea\"}}")
    expected = json.loads("{\"key\": \"c26af2a46d6e4cb7dd8fb5dd48dd0b6886fed0246faf5ccbb884e585c61ccc31\"}")
    assert run(request, ROOT) == expected


def test_delay_seconds():
    request = json.loads("{\"operation\": \"retry_delay\", \"retry_after\": \"12\", \"now\": \"2025-01-01T00:00:00Z\"}")
    expected = json.loads("{\"seconds\": 12}")
    assert run(request, ROOT) == expected


def test_delay_http_date():
    request = json.loads("{\"operation\": \"retry_delay\", \"retry_after\": \"Wed, 01 Jan 2025 00:00:30 GMT\", \"now\": \"2025-01-01T00:00:00Z\"}")
    expected = json.loads("{\"seconds\": 30}")
    assert run(request, ROOT) == expected


def test_retry_then_success():
    request = json.loads("{\"operation\": \"retry\", \"statuses\": [503, 200]}")
    expected = json.loads("{\"status\": 200, \"attempts\": 2}")
    assert run(request, ROOT) == expected


def test_exhaustion_is_error():
    request = json.loads("{\"operation\": \"retry\", \"statuses\": [503, 503, 503, 200]}")
    with pytest.raises(ValueError, match='retry exhausted'):
        run(request, ROOT)


def test_client_error_not_retried():
    request = json.loads("{\"operation\": \"retry\", \"statuses\": [400, 200]}")
    expected = json.loads("{\"status\": 400, \"attempts\": 1}")
    assert run(request, ROOT) == expected


def test_stream_lifetime():
    request = json.loads("{\"operation\": \"stream\", \"body\": \"{\\\"bytes\\\":[1,2]}\"}")
    expected = json.loads("{\"bytes\": [1, 2]}")
    assert run(request, ROOT) == expected


def test_repeated_query():
    request = json.loads("{\"operation\": \"query\", \"params\": {\"tag\": [\"a b\", \"c\"], \"q\": \"x+y\"}}")
    expected = json.loads("{\"query\": \"q=x%2By&tag=a+b&tag=c\"}")
    assert run(request, ROOT) == expected


def test_mixed_batch():
    request = json.loads("{\"operation\": \"batch\", \"results\": [{\"id\": \"a\", \"status\": 201}, {\"id\": \"b\", \"status\": 422}]}")
    expected = json.loads("{\"status\": 207, \"accepted\": [\"a\"], \"failed\": [\"b\"]}")
    assert run(request, ROOT) == expected


def test_all_failed_batch():
    request = json.loads("{\"operation\": \"batch\", \"results\": [{\"id\": \"x\", \"status\": 500}]}")
    expected = json.loads("{\"status\": 502, \"accepted\": [], \"failed\": [\"x\"]}")
    assert run(request, ROOT) == expected


def test_all_accepted_batch():
    request = json.loads("{\"operation\": \"batch\", \"results\": [{\"id\": \"x\", \"status\": 200}]}")
    expected = json.loads("{\"status\": 200, \"accepted\": [\"x\"], \"failed\": []}")
    assert run(request, ROOT) == expected


def test_cursor_multiple_hops():
    request = json.loads("{\"pages\": {\"\": {\"items\": [], \"next_cursor\": 0}, \"0\": {\"items\": [1], \"next_cursor\": \"end\"}, \"end\": {\"items\": [2], \"next_cursor\": null}}}")
    expected = json.loads("{\"items\": [1, 2], \"count\": 2}")
    assert run(request, ROOT) == expected
