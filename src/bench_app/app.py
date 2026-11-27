def run(request, root):
    import base64
    import io
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for entry in sorted(request["entries"], key=lambda item: item["name"]):
            member = zipfile.ZipInfo(entry["name"], (2024, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = 0o644 << 16
            member.compress_type = zipfile.ZIP_STORED
            archive.writestr(member, entry["content"].encode("utf-8"))
    return {"zip_b64": base64.b64encode(output.getvalue()).decode("ascii")}
