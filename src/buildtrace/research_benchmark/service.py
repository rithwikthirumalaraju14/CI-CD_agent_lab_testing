"""Versioned, entirely local service/client project and dependency fixtures."""

from __future__ import annotations

import hashlib
import json

from .common import common_files, fixture_wheel, json_bytes, text
from .model import Check, Seed


CLIENT_V1 = 'client = wirekit.Client("local-fixture-token", timeout=deployment["timeout_seconds"])'
CLIENT_V2 = 'client = wirekit.Client(credential="local-fixture-token", timeout_seconds=deployment["timeout_seconds"])'

WIRE_COMMON = r'''
import hashlib
import json


class Response:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def __bool__(self):
        return self.status < 400

    @property
    def body(self):
        if self.closed:
            raise ValueError("response closed")
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def sign_payload(value):
    """Legacy signing format; retained by the wire transport for old peers."""
    return hashlib.sha256(json.dumps(value).encode("ascii")).hexdigest()
'''

WIRE_V1 = WIRE_COMMON + r'''

__version__ = "1.4.0"


class Client:
    def __init__(self, token, timeout=5):
        if not token or timeout <= 0:
            raise ValueError("invalid client configuration")

    def page(self, pages, cursor):
        return pages[str(cursor)]

    def open(self, body):
        return Response(body=body)
'''

WIRE_V2 = WIRE_COMMON + r'''

__version__ = "2.2.0"


class Client:
    def __init__(self, *, credential, timeout_seconds):
        if not credential or timeout_seconds <= 0:
            raise ValueError("invalid client configuration")

    def page(self, pages, cursor):
        return pages[str(cursor)]

    def open(self, body):
        return Response(body=body)
'''

SIGNATURES = r'''
import hashlib
import json


def sign_payload(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
'''

APP = r'''
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from urllib.parse import urlencode
import wirekit
from .signatures import sign_payload


def decode_response(response):
    content_type = response.headers.get("content-type", "application/json; charset=utf-8")
    charset = "utf-8"
    for parameter in content_type.split(";")[1:]:
        key, _, value = parameter.strip().partition("=")
        if key.lower() == "charset":
            charset = value.strip('"')
    return json.loads(response.body.decode(charset))


def retry_delay(value, now):
    try:
        return max(0, int(value))
    except ValueError:
        target = parsedate_to_datetime(value)
        return max(0, int((target - now).total_seconds()))


def run(request, root):
    deployment = json.loads((root / "context/deployment.json").read_text(encoding="utf-8"))
    if int(wirekit.__version__.split(".")[0]) != deployment["wire_major"]:
        raise ValueError("wirekit major does not match context/deployment.json")
    CLIENT_CONSTRUCTION
    operation = request.get("operation", "sync")
    if operation == "sync":
        pages = request.get("pages", {"": {"items": [], "next_cursor": None}})
        items, cursor = [], ""
        while True:
            page = client.page(pages, cursor)
            items.extend(page["items"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        return {"items": items, "count": len(items)}
    if operation in {"decode", "error", "conditional"}:
        body = request.get("body", "").encode(request.get("body_encoding", "utf-8"))
        response = wirekit.Response(request.get("status", 200), body, request.get("headers"))
        if operation == "error":
            return {"status": response.status if response is not None else 599, "error": decode_response(response)["error"]}
        if operation == "conditional" and response.status == 304:
            return request["cached"]
        return decode_response(response)
    if operation == "sign":
        return {"signature": sign_payload(request["payload"])}
    if operation == "key":
        payload = json.dumps(request["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return {"key": hashlib.sha256(payload).hexdigest()}
    if operation == "retry_delay":
        now = datetime.fromisoformat(request["now"].replace("Z", "+00:00")).astimezone(timezone.utc)
        return {"seconds": retry_delay(request["retry_after"], now)}
    if operation == "retry":
        plan = request["statuses"]
        max_attempts = request.get("max_attempts", deployment["max_attempts"])
        for attempt in range(1, max_attempts + 1):
            status = plan[min(attempt - 1, len(plan) - 1)]
            if status < 500:
                return {"status": status, "attempts": attempt}
        raise ValueError("retry exhausted")
    if operation == "stream":
        with client.open(request["body"].encode("utf-8")) as response:
            body = response.body
        return json.loads(body.decode("utf-8"))
    if operation == "query":
        return {"query": urlencode(sorted(request["params"].items()), doseq=True)}
    if operation == "batch":
        successes = [item["id"] for item in request["results"] if item["status"] < 400]
        failures = [item["id"] for item in request["results"] if item["status"] >= 400]
        status = 207 if successes and failures else (502 if failures else 200)
        return {"status": status, "accepted": successes, "failed": failures}
    raise ValueError("unknown operation")
'''


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def seed(environment: str = "modern") -> Seed:
    legacy = environment in {"legacy", "legacy_opaque"}
    opaque = environment in {"legacy_opaque", "modern_opaque"}
    if environment not in {"legacy", "modern", "default", "legacy_opaque", "modern_opaque"}:
        raise ValueError(f"Unknown service environment: {environment}")
    files = common_files("service", dependency="wirekit>=1,<3" if legacy else "wirekit>=2,<3", pin="wirekit==1.4.0" if legacy else "wirekit==2.2.0")
    files["src/bench_app/app.py"] = text(APP.replace("CLIENT_CONSTRUCTION", CLIENT_V1 if legacy else CLIENT_V2))
    files["src/bench_app/signatures.py"] = text(SIGNATURES)
    files["vendor/wheels/wirekit-1.4.0-py3-none-any.whl"] = fixture_wheel("1.4.0", WIRE_V1)
    files["vendor/wheels/wirekit-2.2.0-py3-none-any.whl"] = fixture_wheel("2.2.0", WIRE_V2)
    files["context/dependencies/wirekit-1.4.0.py"] = text(WIRE_V1)
    files["context/dependencies/wirekit-2.2.0.py"] = text(WIRE_V2)
    if opaque:
        # The prior training sessions retain the observed API details. New
        # deployments expose the wheel, contract, and CI behavior, but not a
        # source listing or a ready-made migration recipe.
        del files["context/dependencies/wirekit-1.4.0.py"]
        del files["context/dependencies/wirekit-2.2.0.py"]
    files["context/deployment.json"] = json_bytes({"wire_major": 1 if legacy else 2, "timeout_seconds": 5, "max_attempts": 3, "network": "disabled; fixture transport only"})
    files["context/contract.md"] = text('''
        # Current client contract

        wirekit is a small bundled, synthetic offline transport, not a real vendor
        SDK. Its two real installable wheels and their readable source are included.
        context/deployment.json is authoritative for the wire major. Major 1 is
        required by legacy peers; major 2 is required by current batch peers.
        The dependency range and constraints must both admit the deployed major.
        v1: Client(token, timeout=5). v2: Client(*, credential, timeout_seconds).

        Pagination stops only at null, not at a false opaque cursor such as the
        number 0. Fixture page keys are strings; cursor values select their str()
        key, with the empty string reserved for the initial page. Fixtures contain
        finite cursor graphs. Decode JSON using
        the Content-Type charset, default UTF-8; preserve all Unicode text.
        An error Response is false in boolean context but is still present; retain
        its original HTTP status and error. A 304 carries no JSON; reuse cached
        content. A fresh 200 must replace cached content.

        Application signatures use bench_app.signatures.sign_payload (canonical
        UTF-8 JSON). wirekit.sign_payload intentionally implements a different
        legacy transport format. Idempotency keys use SHA-256 of canonical JSON:
        sorted keys, compact separators, ensure_ascii=false; never repr(dict).
        Retry-After permits integer seconds or an RFC 7231 HTTP date relative to
        the supplied UTC now; past dates clamp to zero. max_attempts counts the
        initial attempt. Retry only 5xx, and raise ValueError("retry exhausted")
        at exhaustion. Read streaming bodies before leaving their context.
        Query parameters are sorted by key and list values become repeated keys.
        Batch reports retain successes AND failures: mixed=207, all failed=502,
        otherwise=200. No network is used for any operation.
    ''')
    files["context/deployment-notes.md"] = text('''
        # Compatibility matrix

        A legacy on-premises peer is limited to wirekit major 1. A hosted peer
        requires major 2. Neither a universal upgrade nor a universal downgrade
        is valid: keep the selected major and constructor API together.
        JSON-only peers use UTF-8; an older reporting peer still sends Latin-1
        with a charset parameter. The legacy transport signature has spaces and
        insertion-order keys; the current application signature does not.
        This is compatibility documentation, not an agent trajectory.
    ''')
    if opaque:
        files["context/contract.md"] = text('''
            # Current client contract

            The bundled wirekit wheel must match context/deployment.json. The
            project requirement and CI constraint must both admit that major.
            The v1 and v2 client constructors are incompatible. This deployment
            does not include dependency source or migration notes; CI failures
            and the wheel are the available current observations.

            Pagination stops only at null, including when a cursor is zero.
            Decode JSON with the declared Content-Type charset, default UTF-8.
            Retain error response status even when a Response is false in a
            boolean context. A 304 reuses cached data; a 200 replaces it.
            All other application behavior follows the protected public tests.
        ''')
        files["context/deployment-notes.md"] = text('''
            # Deployment context

            A legacy peer uses wirekit major 1; a current peer uses major 2.
            The offline wheelhouse includes both. Select the deployed major and
            adapt application calls after inspecting the observed failure.
        ''')
    public = (
        Check("empty_sync", {}, {"items": [], "count": 0}),
        Check("single_page", {"pages": {"": {"items": ["a"], "next_cursor": None}}}, {"items": ["a"], "count": 1}),
        Check("opaque_zero_cursor", {"pages": {"": {"items": ["a"], "next_cursor": 0}, "0": {"items": ["b"], "next_cursor": None}}}, {"items": ["a", "b"], "count": 2}),
        Check("utf8_response", {"operation": "decode", "body": '{"name":"雪"}'}, {"name": "雪"}),
        Check("declared_charset", {"operation": "decode", "body": '{"name":"café"}', "body_encoding": "iso-8859-1", "headers": {"content-type": "application/json; charset=iso-8859-1"}}, {"name": "café"}),
        Check("error_status_preserved", {"operation": "error", "status": 429, "body": '{"error":"slow down"}'}, {"status": 429, "error": "slow down"}),
        Check("cached_not_modified", {"operation": "conditional", "status": 304, "cached": {"items": [1]}}, {"items": [1]}),
        Check("fresh_replaces_cache", {"operation": "conditional", "status": 200, "body": '{"items":[2]}', "cached": {"items": [1]}}, {"items": [2]}),
        Check("application_signature", {"operation": "sign", "payload": {"b": 2, "a": 1}}, {"signature": digest({"a": 1, "b": 2})}),
        Check("canonical_idempotency", {"operation": "key", "payload": {"z": 0, "a": "雪"}}, {"key": digest({"a": "雪", "z": 0})}),
        Check("delay_seconds", {"operation": "retry_delay", "retry_after": "12", "now": "2025-01-01T00:00:00Z"}, {"seconds": 12}),
        Check("delay_http_date", {"operation": "retry_delay", "retry_after": "Wed, 01 Jan 2025 00:00:30 GMT", "now": "2025-01-01T00:00:00Z"}, {"seconds": 30}),
        Check("retry_then_success", {"operation": "retry", "statuses": [503, 200]}, {"status": 200, "attempts": 2}),
        Check("exhaustion_is_error", {"operation": "retry", "statuses": [503, 503, 503, 200]}, error="retry exhausted"),
        Check("client_error_not_retried", {"operation": "retry", "statuses": [400, 200]}, {"status": 400, "attempts": 1}),
        Check("stream_lifetime", {"operation": "stream", "body": '{"bytes":[1,2]}'}, {"bytes": [1, 2]}),
        Check("repeated_query", {"operation": "query", "params": {"tag": ["a b", "c"], "q": "x+y"}}, {"query": "q=x%2By&tag=a+b&tag=c"}),
        Check("mixed_batch", {"operation": "batch", "results": [{"id": "a", "status": 201}, {"id": "b", "status": 422}]}, {"status": 207, "accepted": ["a"], "failed": ["b"]}),
        Check("all_failed_batch", {"operation": "batch", "results": [{"id": "x", "status": 500}]}, {"status": 502, "accepted": [], "failed": ["x"]}),
        Check("all_accepted_batch", {"operation": "batch", "results": [{"id": "x", "status": 200}]}, {"status": 200, "accepted": ["x"], "failed": []}),
    )
    oracle = (
        Check("cursor_multiple_hops", {"pages": {"": {"items": [], "next_cursor": 0}, "0": {"items": [1], "next_cursor": "end"}, "end": {"items": [2], "next_cursor": None}}}, {"items": [1, 2], "count": 2}),
        Check("quoted_upper_charset", {"operation": "decode", "body": '{"name":"über"}', "body_encoding": "iso-8859-1", "headers": {"content-type": 'application/json; Charset="ISO-8859-1"'}}, {"name": "über"}),
        Check("error_not_absence", {"operation": "error", "status": 503, "body": '{"error":"maintenance"}'}, {"status": 503, "error": "maintenance"}),
        Check("cache_empty_object", {"operation": "conditional", "status": 304, "cached": {}}, {}),
        Check("signature_unicode", {"operation": "sign", "payload": {"é": [1, None], "a": True}}, {"signature": digest({"é": [1, None], "a": True})}),
        Check("key_insertion_independent", {"operation": "key", "payload": {"a": "雪", "z": 0}}, {"key": digest({"a": "雪", "z": 0})}),
        Check("key_nested_canonical", {"operation": "key", "payload": {"b": {"z": 2, "a": 1}}}, {"key": digest({"b": {"z": 2, "a": 1}})}),
        Check("past_date_clamps", {"operation": "retry_delay", "retry_after": "Tue, 31 Dec 2024 23:59:00 GMT", "now": "2025-01-01T00:00:00Z"}, {"seconds": 0}),
        Check("date_not_constant", {"operation": "retry_delay", "retry_after": "Wed, 01 Jan 2025 00:02:00 GMT", "now": "2025-01-01T00:00:15Z"}, {"seconds": 105}),
        Check("one_attempt_only", {"operation": "retry", "statuses": [503, 200], "max_attempts": 1}, error="retry exhausted"),
        Check("success_on_last_attempt", {"operation": "retry", "statuses": [503, 503, 204]}, {"status": 204, "attempts": 3}),
        Check("unicode_stream", {"operation": "stream", "body": '{"label":"雪"}'}, {"label": "雪"}),
        Check("empty_query_list", {"operation": "query", "params": {"a": [], "b": ["", "&"]}}, {"query": "b=&b=%26"}),
        Check("failed_batch_order", {"operation": "batch", "results": [{"id": "z", "status": 400}, {"id": "a", "status": 503}, {"id": "ok", "status": 202}]}, {"status": 207, "accepted": ["ok"], "failed": ["z", "a"]}),
    )
    example = {"pages": {"": {"items": ["alpha"], "next_cursor": 0}, "0": {"items": ["beta"], "next_cursor": None}}}
    return Seed(files, public, oracle, example, {"items": ["alpha", "beta"], "count": 2})
