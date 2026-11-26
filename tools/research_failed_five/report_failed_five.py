"""Write the failed-five learning audit from saved trials and WikiTrace corpora."""

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "CI-CD_agent_lab_testing").is_dir()
            and (parent / "data").is_dir())
OUT = ROOT / "deliverables"
BASE = ROOT / "CI-CD_agent_lab_testing/output/learning50-final"
SOURCES = (
    ROOT / "data/learning50-v2-five/experiments/results.jsonl",
    ROOT / "data/learning-failed-v3/experiments/results.jsonl",
)
CORPUS = BASE / "memory-failed-five-combined/corpus.json"
ORIGINAL = BASE / "memory-learning50-v1/corpus.json"
PRICES = {"uncached_input": 0.10, "cached_input": 0.01, "output": 0.50}
PRICING_URL = "https://developers.openai.com/api/docs/models/gpt-6-luna"


def estimate(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    return round(((input_tokens - cached_tokens) * PRICES["uncached_input"]
                  + cached_tokens * PRICES["cached_input"] + output_tokens * PRICES["output"]) / 1_000_000, 6)


def main() -> None:
    admission = json.loads((OUT / "FAILED_FIVE_ADMISSION.json").read_text(encoding="utf-8"))
    old_hash = hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()
    if old_hash != admission["original_corpus_sha256"]:
        raise ValueError("Original learning-50 corpus changed")
    rows = [json.loads(line) for source in SOURCES for line in source.read_text(encoding="utf-8").splitlines() if line]
    memory = json.loads(CORPUS.read_text(encoding="utf-8"))
    if len(rows) != 24 or len(memory["training_tasks"]) != 15:
        raise ValueError("Expected 24 diagnostic trials and 15 admitted learning sessions")
    by_task = defaultdict(list)
    solver = Counter()
    attempts = []
    for row in rows:
        by_task[row["task_id"]].append(row)
        for key in ("model_calls", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            solver[key] += row[key]
        verification = row["verifications"][-1] if row["verifications"] else {}
        attempts.append({
            "task": row["task_id"], "trial": row["trial"] + 1, "outcome": row["outcome"],
            "public_ci_passed": verification.get("workflow_conclusion") == "success" and verification.get("required_checks_passed") is True,
            "trusted_oracle_passed": verification.get("oracle_passed") is True,
            "model_calls": row["model_calls"], "input_tokens": row["input_tokens"],
            "cached_input_tokens": row["cached_input_tokens"], "output_tokens": row["output_tokens"],
            "reasoning_tokens_in_output": row["reasoning_tokens"],
            "estimated_model_cost_usd": estimate(row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]),
            "initial_ci": next(task["initial_run_url"] for task in json.loads((ROOT / "data/learning50-v2-five/experiments/experiment.json" if row["task_id"].startswith("V2") else ROOT / "data/learning-failed-v3/experiments/experiment.json").read_text(encoding="utf-8"))["tasks"] if task["id"] == row["task_id"]),
            "candidate_ci": verification.get("run_url", ""), "trusted_oracle_ci": verification.get("oracle_run_url", ""),
            "source_results": str(next(source for source in SOURCES if row["id"] in source.read_text(encoding="utf-8")).relative_to(ROOT)),
            "trajectory": row["trajectory_path"],
        })
    attempts.sort(key=lambda value: (value["task"], value["trial"]))
    with (OUT / "FAILED_FIVE_ATTEMPTS.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(attempts[0]))
        writer.writeheader()
        writer.writerows(attempts)
    compiler = Counter()
    for call in memory["compiler_calls"]:
        compiler["model_calls"] += 1
        for key in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens"):
            compiler[key] += call[key]
    solver_cost = estimate(solver["input_tokens"], solver["cached_input_tokens"], solver["output_tokens"])
    compiler_cost = estimate(compiler["prompt_tokens"], compiler["cached_tokens"], compiler["completion_tokens"])
    report = {
        "original_learning50_corpus_sha256": old_hash,
        "failed_five_corpus_manifest_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "tasks": {task: {"attempts": len(group), "verified_solves": sum(row["outcome"] == "solved" for row in group)}
                  for task, group in sorted(by_task.items())},
        "diagnostic_trials": len(rows), "admitted_sessions": len(memory["training_tasks"]), "insights": len(memory["insights"]),
        "pricing_url": PRICING_URL, "price_per_million_tokens_usd": PRICES,
        "solver_usage": dict(solver), "solver_estimated_model_cost_usd": solver_cost,
        "compiler_usage": dict(compiler), "compiler_estimated_model_cost_usd": compiler_cost,
        "combined_estimated_model_cost_usd": round(solver_cost + compiler_cost, 6),
        "cost_note": "Text-token estimate from recorded usage, not an invoice; cache-write premiums and GitHub billing are not included.",
    }
    (OUT / "FAILED_FIVE_USAGE.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Failed-five learning question repair",
        "",
        "Status: **complete for these five replacements**. The pre-existing 45 question tasks, their trials, and the original learning-50 corpus were not edited.",
        "",
        "## Admission result",
        "",
        "| Original failed question | Replacement | No-memory verified solves | Corpus admission |",
        "| --- | --- | ---: | --- |",
        "| L06: dependency major and query keys | V2L06 | 3/3 | admitted |",
        "| L25: archive metadata and byte hashing | V3L25 | 3/3 | admitted |",
        "| L27: canonical duplicate TAR members | V3L27 | 3/3 | admitted |",
        "| L30: cross-OS unsafe TAR paths | V3L30 | 3/3 | admitted |",
        "| L45: required build gate | V2L45 | 3/3 | admitted |",
        "",
        "Each counted solve passed both the public CI checks and the trusted oracle. V2L25, V2L27, and V2L30 each failed 0/3 and were excluded; their narrower V3 replacements passed. The admission rule was at least one verified solve in three independent No-memory attempts. All 15 admitted sessions were successful.",
        "",
        "The new five-question WikiTrace corpus is `CI-CD_agent_lab_testing/output/learning50-final/memory-failed-five-combined/`. It has 15 raw solver histories, original CI evidence, 15 compiled trajectories, and 31 linked insights. All 31 insights passed linked compilation and raw-document reads. Source corpus hashes and the original corpus hash are in `merge-provenance.json`.",
        "",
        "The original learning-50 corpus remains at `CI-CD_agent_lab_testing/output/learning50-final/memory-learning50-v1/`; its manifest SHA-256 is `" + old_hash + "`. This supplement is separate and has not been attached to a development or held-out evaluation. The old 125/150 (83.3%) calibration still describes the original questions; it is not a new baseline for the revised five.",
        "",
        "The earlier goals of 55–85% overall success and at least one success for every question cannot both be inferred from the frozen 45 plus replacements: the original 45 already account for 125 successes, and one success on each of five replacements would make at least 130/150 (86.7%). This round prioritizes the requested five-question admission rule; no mixed-version 50-question score is reported as a fresh baseline.",
        "",
        "## Recorded usage and estimated cost",
        "",
        f"Across all 24 diagnostic trials (including the nine excluded V2 failures): {solver['model_calls']} solver model calls, {solver['input_tokens']:,} input tokens ({solver['cached_input_tokens']:,} cached), and {solver['output_tokens']:,} output tokens; estimated **${solver_cost:.6f}**.",
        "",
        f"Across 15 WikiTrace compiler calls: {compiler['prompt_tokens']:,} input tokens ({compiler['cached_tokens']:,} cached) and {compiler['completion_tokens']:,} output tokens; estimated **${compiler_cost:.6f}**. Combined estimate: **${solver_cost + compiler_cost:.6f}**.",
        "",
        f"Prices: [GPT-6 Luna official model page]({PRICING_URL}), USD per million text tokens: $0.10 input, $0.01 cached input, $0.50 output. Reasoning tokens are included in output. This is a model-token estimate from recorded usage, not an invoice; cache-write premiums and GitHub billing are not included.",
        "",
        "Every attempt, outcome, CI/oracle URL, token count, cost estimate, and trajectory path is in `FAILED_FIVE_ATTEMPTS.csv`; machine-readable totals are in `FAILED_FIVE_USAGE.json`.",
        "",
    ]
    (OUT / "FAILED_FIVE_LEARNING_RESULT.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"tasks": 5, "admitted_sessions": 15, "insights": 31,
                      "estimated_model_cost_usd": report["combined_estimated_model_cost_usd"]}, indent=2))


if __name__ == "__main__":
    main()
