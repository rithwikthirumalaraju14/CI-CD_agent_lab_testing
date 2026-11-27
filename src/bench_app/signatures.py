import hashlib
import json


def canonical_digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def sign_payload(value):
    return canonical_digest(value)
