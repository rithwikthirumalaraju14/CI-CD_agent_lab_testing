def run(request, root):
    import hashlib

    digest = hashlib.sha256()
    for name, value in sorted(request["files"].items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return {"digest": digest.hexdigest()}
