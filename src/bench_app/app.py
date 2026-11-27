def run(request, root):
    build = request["build"]
    candidate = request["candidate"]
    promoted = [event for event in request["ledger"] if event["kind"] == "promoted"]
    active = promoted[-1]["digest"] if promoted else None

    # A tag can be reused, so deployment provenance must be tied to the
    # exact artifact that passed CI rather than inferred from the tag.
    if not (
        request["tested_source"] == build["source"] == candidate["source"]
        and build["digest"] == candidate["digest"]
    ):
        return {"status": "rejected", "active_digest": active}
    if request["health"] != "ok":
        return {"status": "failed", "active_digest": active}
    return {"status": "promoted", "active_digest": candidate["digest"]}
