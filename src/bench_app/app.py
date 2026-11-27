import hashlib
import json

def run(request, root):
    manifest = request["manifest"]
    payload = json.dumps(manifest, sort_keys=False, separators=(",", ":"), ensure_ascii=True)
    return {"signature": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
