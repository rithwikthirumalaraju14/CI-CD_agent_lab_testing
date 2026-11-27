import json
from importlib.resources import files

def run(request, root):
    data = files("bench_app").joinpath("data/policy.json").read_text(encoding="utf-8")
    policy = json.loads(data)
    return {key: policy[key] for key in request["fields"]}
