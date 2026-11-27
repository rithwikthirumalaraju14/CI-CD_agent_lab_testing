def run(request, root):
    build = request["build"]
    candidate = request["candidate"]

    # A build receipt means CI passed, not that the artifact is serving.
    # Only promoted receipts describe the deployed release.
    promoted = [event for event in request["ledger"] if event["kind"] == "promoted"]
    active = promoted[-1]["digest"] if promoted else None

    # Source provenance alone is insufficient: the deployed artifact must be
    # the exact artifact whose build passed CI.
    if not (
        request["tested_source"] == build["source"] == candidate["source"]
        and candidate["digest"] == build["digest"]
    ):
        return {"status": "rejected", "active_digest": active}
    if request["health"] != "ok":
        return {"status": "failed", "active_digest": active}
    return {"status": "promoted", "active_digest": candidate["digest"]}
