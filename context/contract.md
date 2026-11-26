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
