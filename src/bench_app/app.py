import json

def run(request, root):
    runners = json.loads((root / "context/probe-settings.json").read_text())
    settings = runners[request["runner"]]
    if "max_attempts" in settings:
        allowed = settings["max_attempts"]
    else:
        # Retries are additional to the initial readiness probe.
        allowed = settings["retries"] + 1
    probes = request["responses"][:allowed]
    for number, status in enumerate(probes, 1):
        if status == 200:
            return {"action":"promote","attempts":number}
    return {"action":"rollback","attempts":len(probes)}
