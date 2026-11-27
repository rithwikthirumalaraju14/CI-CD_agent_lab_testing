def run(request, root):
    import base64
    import io
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for entry in request["entries"]:
            member = zipfile.ZipInfo(entry["name"], tuple(request["runner_clock"]))
            member.create_system = 3
            member.external_attr = entry["mode"] << 16
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, entry["content"].encode("utf-8"))
    return {"zip_b64": base64.b64encode(output.getvalue()).decode("ascii")}
