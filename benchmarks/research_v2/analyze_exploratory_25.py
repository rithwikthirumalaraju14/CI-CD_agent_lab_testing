"""Summarize the completed exploratory 25-case run without changing trials."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from random import Random
import json
import sys

from buildtrace.dashboard import MODES, load_study


ROOT = Path("data")
SLUG = "exploratory-25/dev-v1"


def paired_difference(rows: list[dict], case_ids: set[str], baseline: str) -> float:
    index = {(row["task_id"], row["trial"], row["mode"]): row for row in rows if row["task_id"] in case_ids}
    comparisons = [
        (int(index[case_id, trial, "wikitrace"]["outcome"] == "solved"),
         int(index[case_id, trial, baseline]["outcome"] == "solved"))
        for case_id in sorted(case_ids) for trial in range(3)
    ]
    return sum(wiki - other for wiki, other in comparisons) / len(comparisons)


def family_interval(rows: list[dict], cases: list[dict], baseline: str, *, draws: int = 20_000) -> dict:
    clusters = defaultdict(set)
    for case in cases:
        clusters[case["family"]].add(case["id"])
    families = sorted(clusters)
    rng = Random(42)
    observed = paired_difference(rows, {case["id"] for case in cases}, baseline)
    replicates = []
    index = {(row["task_id"], row["trial"], row["mode"]): row for row in rows}
    for _ in range(draws):
        selected = [clusters[families[rng.randrange(len(families))]] for _ in families]
        numerator = denominator = 0
        for cluster in selected:
            for case_id in cluster:
                for trial in range(3):
                    numerator += int(index[case_id, trial, "wikitrace"]["outcome"] == "solved")
                    numerator -= int(index[case_id, trial, baseline]["outcome"] == "solved")
                    denominator += 1
        replicates.append(numerator / denominator)
    replicates.sort()
    return {
        "difference_percentage_points": round(observed * 100, 1),
        "cluster_95_percent_interval_percentage_points": [
            round(replicates[int(0.025 * draws)] * 100, 1),
            round(replicates[int(0.975 * draws) - 1] * 100, 1),
        ],
        "family_clusters": len(families), "resamples": draws,
    }


def summarize_subset(dashboard: dict, cases: list[dict]) -> dict:
    case_ids = {case["id"] for case in cases}
    rows = [row for row in dashboard["trials"] if row["task_id"] in case_ids]
    labels = {case["id"]: case["label"] for case in cases}
    results = {"cases": len(cases), "trials": len(rows), "families": len({case["family"] for case in cases}),
               "domains": dict(Counter(case["domain"] for case in cases)), "modes": {}, "paired": {}}
    for mode in MODES:
        group = [row for row in rows if row["mode"] == mode]
        gained = [row for row in group if labels[row["task_id"]] == "baseline-unsolved"]
        regressed = [row for row in group if labels[row["task_id"]] == "baseline-solved"]
        results["modes"][mode] = {
            "verified_fixes": sum(row["outcome"] == "solved" for row in group),
            "attempts": len(group), "outcomes": dict(Counter(row["outcome"] for row in group)),
            "strict_gain": None if mode == "no-memory" else {"solved": sum(row["outcome"] == "solved" for row in gained), "eligible": len(gained)},
            "strict_regression": None if mode == "no-memory" else {"failed": sum(row["outcome"] != "solved" for row in regressed), "eligible": len(regressed)},
            "billable_tokens": sum(row["billable_tokens"] for row in group),
            "input_tokens": sum(row["input_tokens"] for row in group),
            "cached_input_tokens": sum(row["cached_input_tokens"] for row in group),
            "output_tokens": sum(row["output_tokens"] for row in group),
            "reasoning_tokens_inside_output": sum(row["reasoning_tokens"] for row in group),
            "estimated_solver_api_usd": round(sum(row["cost_usd"] or 0 for row in group), 6),
            "unknown_cost_attempts": sum(row["cost_usd"] is None for row in group),
            "history_requests": sum(row["history_requests"] for row in group),
            "raw_history_deliveries": sum(row["raw_deliveries"] for row in group),
            "reviewed_adopted_effects": sum(row["reviewed_adopted_effects"] for row in group),
            "reviewed_evidence_bursts": sum(row["reviewed_evidence_bursts"] for row in group),
            "pending_evidence_bursts": sum(row["evidence_bursts"] - row["reviewed_evidence_bursts"] for row in group),
            "reviewed_unnecessary_hunks": sum(row["unnecessary_hunks"] for row in group),
            "unreviewed_hunks": sum(row["unreviewed_hunks"] for row in group),
        }
    index = {(row["task_id"], row["trial"], row["mode"]): row for row in rows}
    for baseline in ("no-memory", "insights-only"):
        counts = Counter()
        for case_id in case_ids:
            for trial in range(3):
                wiki = index[case_id, trial, "wikitrace"]["outcome"]
                other = index[case_id, trial, baseline]["outcome"]
                if wiki not in {"solved", "unsolved"} or other not in {"solved", "unsolved"}:
                    counts["infrastructure_affected"] += 1
                elif wiki == other:
                    counts["tie"] += 1
                elif wiki == "solved":
                    counts["wikitrace_win"] += 1
                else:
                    counts["wikitrace_loss"] += 1
        results["paired"][baseline] = dict(counts)
    return results


def main() -> int:
    dashboard = load_study(ROOT, SLUG)
    if dashboard["planned_cases"] != 25 or dashboard["planned_trials"] != 225 or dashboard["completed_trials"] != 225:
        raise RuntimeError(f"The declared 225 trials are not complete ({dashboard['completed_trials']}/225)")
    standard = [case for case in dashboard["cases"] if not case["composite"]]
    hard = [case for case in dashboard["cases"] if case["composite"]]
    if len(standard) != 24 or len(hard) != 1:
        raise RuntimeError("The declared 24 standard + 1 hard case mix changed")
    report = {
        "classification": "exploratory development; earlier source-use gate failed",
        "analysis_timing": "This summary procedure was written after 9 of 225 trial results were visible.",
        "experiment_id": dashboard["id"], "completed_trials": 225,
        "all_25_descriptive": summarize_subset(dashboard, dashboard["cases"]),
        "standard_24": summarize_subset(dashboard, standard),
        "hard_case": summarize_subset(dashboard, hard),
        "standard_family_clustered_success_difference": {
            baseline: family_interval(dashboard["trials"], standard, baseline)
            for baseline in ("no-memory", "insights-only")
        },
        "notes": [
            "The hard composite shares four failure mechanisms with standard cases, so it is shown separately in the clustered interval.",
            "Strict Gain and Regression require all three valid no-memory attempts on a case to agree.",
            "Infrastructure outcomes remain in fix counts as unsuccessful attempts and are reported separately.",
            "Reasoning tokens are included within output tokens; model API cost is estimated from recorded solver usage only.",
            "Unreviewed patches or evidence bursts are pending assessment, not zero unnecessary changes or decision changes.",
            "This authored benchmark is exploratory because the prior development source-use gate failed; it does not establish held-out superiority.",
        ],
        "source_files": dashboard["source_files"],
    }
    output = Path("data/exploratory-25/analysis/dev-v1")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "summary.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"summary": str(output / "summary.json"), "completed": 225,
                      "standard_clusters": report["standard_family_clustered_success_difference"]["no-memory"]["family_clusters"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
