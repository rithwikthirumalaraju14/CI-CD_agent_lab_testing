def run(request, root):
    accepted, failed, calls = [], [], {}
    for service in request["services"]:
        name = service["id"]
        state = service["prior"]
        calls[name] = 0
        if state in ("new", "retryable"):
            for status in service["events"][:request["max_attempts"]]:
                calls[name] += 1
                if 200 <= status < 300:
                    state = "accepted"
                    break
                if 500 <= status < 600:
                    state = "retryable"
                    continue
                state = "rejected"
                break
        if state == "accepted":
            accepted.append(name)
        else:
            failed.append(name)
    if accepted and failed:
        status = 207
    elif failed:
        status = 502
    else:
        status = 200
    return {"status": status, "accepted": accepted, "failed": failed, "calls": calls}
