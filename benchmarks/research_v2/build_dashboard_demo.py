"""Build a clearly labeled, deterministic dashboard demo from 25 case names.

No solver, model, CI, oracle, or review work is performed by this script.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "data" / "exploratory-25" / "experiments" / "dev-v1" / "experiment.json"
DEFAULT_OUTPUT = ROOT / "data" / "generated-example" / "experiments" / "v2"
MODES = ("no-memory", "insights-only", "wikitrace")


def _success_count(case_index: int, mode: str) -> int:
    if mode == "no-memory":
        return 3 if case_index < 6 else 2 if case_index < 16 else 1 if case_index < 22 else 0
    if mode == "insights-only":
        return 3 if case_index < 10 else 2 if case_index < 22 else 1
    return 3 if case_index < 17 else 2 if case_index < 24 else 1


def build(output: Path = DEFAULT_OUTPUT) -> dict[str, int]:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    tasks = [{"id": task["id"], "family": task.get("family"), "split": "dev"}
             for task in source["tasks"]]
    if len(tasks) != 25 or len({task["id"] for task in tasks}) != 25:
        raise ValueError("The source specification must contain 25 distinct cases")

    spec = {
        "schema_version": 2,
        "id": "exploratory-25-development-v2",
        "data_kind": "generated-example",
        "description": "Illustrative, generated outcomes; not observed solver or CI results.",
        "scenario_source": "Case IDs and families from exploratory-25/dev-v1; no trial results copied.",
        "model": {"provider": "illustrative", "model": "gpt-6-luna"},
        "trials": 3,
        "tasks": tasks,
    }
    if output.exists():
        existing = output / "experiment.json"
        if not existing.is_file() or json.loads(existing.read_text(encoding="utf-8")).get("data_kind") != "generated-example":
            raise ValueError(f"Refusing to overwrite a non-demo directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "experiment.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    records = []
    counts = {mode: 0 for mode in MODES}
    for case_index, task in enumerate(tasks):
        for trial in range(3):
            for mode_index, mode in enumerate(MODES):
                success_count = _success_count(case_index, mode)
                attempt_shift = {"no-memory": 0, "insights-only": 2, "wikitrace": 1}[mode]
                solved = ((trial - case_index - attempt_shift) % 3) < success_count
                counts[mode] += int(solved)
                difficulty = case_index // 5
                calls = 6 + difficulty + trial + (0 if mode == "wikitrace" else mode_index + 1)
                input_tokens = 48_000 + difficulty * 5_200 + trial * 2_300 + mode_index * 1_100
                cached_tokens = round(input_tokens * (0.70 + 0.02 * mode_index))
                output_tokens = 1_300 + difficulty * 150 + trial * 90 + mode_index * 45
                wall_seconds = 120 + difficulty * 24 + trial * 13 + (20 if not solved else 0) - mode_index * 9
                ci_wait_seconds = 65 + difficulty * 6 + trial * 3
                reads = ([{"status": "delivered", "layer": "raw"}]
                         if mode == "wikitrace" and case_index >= 6 else [])
                records.append({
                    "schema_version": 2,
                    "data_kind": "generated-example",
                    "id": f"demo-{case_index + 1:02d}-{mode}-{trial + 1}",
                    "experiment_id": spec["id"],
                    "task_id": task["id"],
                    "mode": mode,
                    "trial": trial,
                    "role": "reference",
                    "outcome": "solved" if solved else "unsolved",
                    "stop_reason": "illustrative-success" if solved else "illustrative-agent-failure",
                    "model": {"provider": "illustrative", "model": "gpt-6-luna"},
                    "returned_models": ["gpt-6-luna"],
                    "timing": {"wall_seconds": wall_seconds, "ci_wait_seconds": ci_wait_seconds,
                               "approval_wait_seconds": 0},
                    "model_calls": calls,
                    "patch_attempts": 1 if solved else 2,
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_tokens": round(output_tokens * 0.12),
                    "evidence_reads": reads,
                    "evidence_decisions": [],
                    "hunks": [],
                    "verifications": [],
                    "packet_insight_ids": [],
                    "trajectory_path": None,
                })
    (output / "results.jsonl").write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in records), encoding="utf-8")
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps({"data_kind": "generated-example", "cases": 25, "attempts_per_agent": 3,
                      "trials": 225, "fixes": build(args.output)}, indent=2))
