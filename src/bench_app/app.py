def run(request, root):
    import hashlib

    digest = hashlib.sha256()
    for name, value in sorted(request["files"].items()):
        digest.update(name.encode() + b":" + value.encode() + b"\n")
    return {"digest": digest.hexdigest()}
