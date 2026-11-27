def run(request, root):
    import hashlib

    digest = hashlib.sha256()
    for name, value in sorted(request["files"].items()):
        digest.update(name.encode("utf-8") + b"\0" + value.encode("utf-8") + b"\0")
    return {"digest": digest.hexdigest()}
