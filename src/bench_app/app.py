def identity(job):
    return f"{job['platform']}-{job['arch']}-{job['revision']}.whl"

def run(request, root):
    published = {identity(job): job["digest"] for job in request["completed_jobs"]}
    target = identity(request["deployment"])
    if target not in published:
        raise ValueError("requested artifact absent")
    return {"digest": published[target]}
