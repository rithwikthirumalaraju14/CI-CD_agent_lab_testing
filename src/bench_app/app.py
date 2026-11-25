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
    client = wirekit.Client(credential="local-fixture-token", timeout_seconds=deployment["timeout_seconds"])
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
