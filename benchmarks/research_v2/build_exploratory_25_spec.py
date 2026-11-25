"""Relabel the declared 25 new cases as exploratory development, never test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from buildtrace.experiments.models import ExperimentSpec


EXPECTED_IDS = {
    *(f"rv2-{n:03d}" for n in range(7, 15)),
    *(f"rv2-{n:03d}" for n in range(21, 29)),
    *(f"rv2-{n:03d}" for n in range(35, 43)),
    "hard-test-001",
}


def convert(source: Path, target: Path) -> ExperimentSpec:
    if target.exists():
        raise ValueError("Choose a new exploratory specification path")
    data = json.loads(source.read_text(encoding="utf-8"))
    if (data.get("schema_version") != 2 or data.get("trials") != 3
            or data.get("model", {}).get("model") != "gpt-6-luna"
            or {task["id"] for task in data["tasks"]} != EXPECTED_IDS
            or any(task["split"] != "test" for task in data["tasks"])):
        raise ValueError("The source specification is not the declared 25-case test catalogue")
    data["id"] = "exploratory-25-development-v1"
    for task in data["tasks"]:
        task["split"] = "dev"
    found = set()
    for identity in data["split_registry"]:
        if identity["id"] in EXPECTED_IDS:
            if identity["split"] != "test":
                raise ValueError("Selected case identity has the wrong original split")
            identity["split"] = "dev"
            found.add(identity["id"])
    if found != EXPECTED_IDS:
        raise ValueError("The split registry does not identify all 25 cases")
    data["retrieval"].update(max_insights=8, max_characters=16000)
    spec = ExperimentSpec.model_validate(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(spec.model_dump_json(indent=2) + "\n")
    return spec


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    result = convert(args.source, args.target)
    print(json.dumps({"id": result.id, "cases": len(result.tasks), "trials_per_mode": result.trials,
                      "selected_split": "dev", "classification": "exploratory"}))
