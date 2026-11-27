def run(request, root):
    build = request["build"]
    candidate = request["candidate"]
    deployed = [event for event in request["ledger"] if event["kind"] in ("promoted", "built")]
    active = deployed[-1]["digest"]
    if not (request["tested_source"] == build["source"] == candidate["source"]):
        return {"status": "rejected", "active_digest": active}
    if request["health"] != "ok":
        return {"status": "failed", "active_digest": active}
    return {"status": "promoted", "active_digest": candidate["digest"]}
