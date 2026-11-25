import hashlib
import json


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def run(request, root):
    operation = request["operation"]
    if operation == "artifact_path":
        return {"path": "dist/" + request["source_sha"] + "/" + request["name"]}
    if operation == "deploy_gate":
        tests_ok = request["tests"] == "success"
        approval_ok = not request.get("manual", False) or request.get("approved", False)
        return {"deploy": tests_ok and approval_ok}
    if operation == "cache_key":
        return {"key": "deps-" + request["os"] + "-" + request["python"] + "-" + _digest(request["lockfile"])}
    if operation == "matrix_artifact":
        name = request["platform"] + "-" + request["arch"] + "-" + request["source_sha"] + ".whl"
        return {"upload_name": name, "download_name": name}
    if operation == "required_checks":
        changed = request["changed"]
        build_needed = any(path == "pyproject.toml" or path.startswith("ci/") or path.startswith("src/") for path in changed)
        checks = ["tests", "build"] if build_needed else ["tests"]
        return {"checks": checks, "release": all(request["status"].get(check) == "success" for check in checks)}
    if operation == "readiness":
        healthy = request["port_open"] and request["health"] == "ok"
        return {"ready": healthy, "job_success": healthy}
    if operation == "release_provenance":
        if request["source_sha"] != request["build_sha"]:
            raise ValueError("release source mismatch")
        return {"published_sha": request["tag_sha"]}
    if operation == "environment_lock":
        if request["test_lock"] != request["build_lock"]:
            raise ValueError("tested and built dependencies differ")
        return {"lock": request["build_lock"]}
    if operation == "deploy_identity":
        if request["tested_sha"] != request["built_sha"] or request["built_sha"] != request["deployed_sha"]:
            raise ValueError("source revision mismatch")
        if request["built_digest"] != request["deployed_digest"]:
            raise ValueError("artifact digest mismatch")
        return {"deployed_sha": request["deployed_sha"], "digest": request["deployed_digest"]}
    if operation == "rollout":
        if request["health"] == "ok":
            return {"status": "success", "active_digest": request["candidate_digest"]}
        return {"status": "failed", "active_digest": request["last_healthy_digest"]}
    raise ValueError("unknown operation")
