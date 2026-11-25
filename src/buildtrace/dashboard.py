"""Read-only experiment dashboard data from saved, locally recorded trials."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
import json


MODES = ("no-memory", "insights-only", "wikitrace")
MODE_LABELS = {"no-memory": "No memory", "insights-only": "Insights only", "wikitrace": "WikiTrace"}


def _classification(spec: dict, slug: str) -> str:
    if spec.get("data_kind") in {"synthetic-demo", "generated-example"}:
        return spec["data_kind"]
    return "exploratory" if slug.startswith(("exploratory-25/", "question-lab/")) else "development" if "dev" in slug else "recorded"
PRICE = {
    "model": "gpt-6-luna",
    "currency": "USD",
    "per_million": {"uncached_input": 0.10, "cached_input": 0.01, "output": 0.50},
    "long_context_threshold_input_tokens": 272_000,
    "long_context_input_multiplier": 2.0,
    "long_context_output_multiplier": 1.5,
    "source": "https://developers.openai.com/api/docs/models/gpt-6-luna",
    "checked_on": "2026-11-21",
    "scope": "Estimated solver Responses API text-token cost only; not a provider invoice.",
    "exclusions": (
        "Unobserved cache-write charges", "CI compute", "Taxes or regional premiums",
        "Separate reviewer and memory-compilation calls",
    ),
}


def _experiment_directories(data_dir: Path) -> dict[str, Path]:
    roots = [data_dir / "experiments", *(p / "experiments" for p in data_dir.iterdir() if p.is_dir())] if data_dir.is_dir() else []
    found: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink() and (child / "experiment.json").is_file():
                slug = ("pilot" if root.parent == data_dir else root.parent.name) + "/" + child.name
                found[slug] = child
    return dict(sorted(found.items()))


def list_studies(data_dir: Path) -> list[dict]:
    studies = []
    for slug, directory in _experiment_directories(data_dir).items():
        try:
            spec = json.loads((directory / "experiment.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # The original one-off demo is retired from the study picker.
        if spec.get("id") == "synthetic-demo-25x3x3-v1":
            continue
        result_file = directory / "results.jsonl"
        studies.append({
            "slug": slug, "id": spec.get("id", slug),
            "planned_cases": len(spec.get("tasks", [])),
            "has_results": result_file.is_file(),
            "updated_at": result_file.stat().st_mtime if result_file.is_file() else (directory / "experiment.json").stat().st_mtime,
            "classification": _classification(spec, slug),
        })
    # Put the requested v2 comparison first in the study picker.
    return sorted(studies, key=lambda item: (item["id"] != "exploratory-25-development-v2", -item["updated_at"]))


def _complete_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    content = path.read_bytes()
    if not content.endswith(b"\n"):
        content = content.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in content else b""
    return content.decode("utf-8").splitlines()


@lru_cache(maxsize=1024)
def _trajectory_usage(path_name: str, size: int, mtime_ns: int) -> tuple[dict, ...]:
    # The file signature invalidates this process-local cache if a trajectory
    # changes; no derived result is written back into the experiment.
    del size, mtime_ns
    rows = []
    for line in _complete_lines(Path(path_name)):
        event = json.loads(line)
        if event.get("type") == "message" and isinstance(event.get("usage"), dict):
            rows.append(event["usage"])
    return tuple(rows)


def _recorded_cost(record: dict, directory: Path) -> tuple[float | None, str]:
    model = record.get("model", {}).get("model")
    returned = record.get("returned_models") or []
    if model != PRICE["model"] or any(not item.startswith(PRICE["model"]) for item in returned):
        return None, "unknown-model"
    if record.get("model_calls", 0) and not (record.get("input_tokens", 0) or record.get("output_tokens", 0)):
        return None, "missing-usage"
    usage_rows = []
    relative = record.get("trajectory_path")
    if isinstance(relative, str):
        path = (directory / relative).resolve()
        if path.is_relative_to(directory.resolve()) and path.is_file():
            signature = path.stat()
            usage_rows.extend(_trajectory_usage(str(path), signature.st_size, signature.st_mtime_ns))
    if usage_rows and all(sum(int(row.get(key, 0)) for row in usage_rows) == int(record.get(key, 0))
                          for key in ("input_tokens", "output_tokens", "cached_input_tokens")):
        method = "per-response"
    else:
        usage_rows = [record]
        method = "record-total"
    price = PRICE["per_million"]
    cost = 0.0
    for row in usage_rows:
        input_tokens = int(row.get("input_tokens", 0))
        cached = int(row.get("cached_input_tokens", 0))
        output = int(row.get("output_tokens", 0))
        if cached < 0 or cached > input_tokens or output < 0:
            raise ValueError("Invalid recorded token usage")
        long = input_tokens > PRICE["long_context_threshold_input_tokens"]
        input_multiplier = PRICE["long_context_input_multiplier"] if long else 1.0
        output_multiplier = PRICE["long_context_output_multiplier"] if long else 1.0
        cost += (
            (input_tokens - cached) * price["uncached_input"] * input_multiplier
            + cached * price["cached_input"] * input_multiplier
            + output * price["output"] * output_multiplier
        ) / 1_000_000
    return round(cost, 8), method


def _case_metadata(task: dict) -> dict:
    try:
        from buildtrace.research_benchmark.catalogue import get_case

        case = get_case(task["id"])
        return {"domain": case.domain, "title": case.title, "original_split": "test" if case.split == "test" else case.split,
                "composite": task["id"].startswith("hard-test-")}
    except (KeyError, ImportError, TypeError):
        return {"domain": task.get("domain", "other"), "title": task.get("title", task.get("id", "Unknown case")),
                "original_split": task.get("split"), "composite": False}


def _safe_github_url(value) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://github.com/") else None


def _strict_labels(by_case: dict[str, list[dict]], repetitions: int) -> dict[str, str]:
    labels = {}
    for case_id, records in by_case.items():
        references = {r["trial"]: r for r in records if r["mode"] == "no-memory" and r.get("role", "reference") == "reference"}
        if set(references) != set(range(repetitions)):
            labels[case_id] = "pending"
        elif all(r["outcome"] == "solved" for r in references.values()):
            labels[case_id] = "baseline-solved"
        elif all(r["outcome"] == "unsolved" for r in references.values()):
            labels[case_id] = "baseline-unsolved"
        else:
            labels[case_id] = "mixed-or-infrastructure"
    return labels


def _metric(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "percent": round(100 * numerator / denominator, 1) if denominator else None}


def load_study(data_dir: Path, slug: str) -> dict:
    directory = _experiment_directories(data_dir).get(slug)
    if directory is None:
        raise KeyError("Unknown experiment")
    spec = json.loads((directory / "experiment.json").read_text(encoding="utf-8"))
    original_records = [json.loads(line) for line in _complete_lines(directory / "results.jsonl")]
    records = original_records
    review_file = directory.parent.parent / "analysis" / directory.name / "fully-reviewed.jsonl"
    if review_file.is_file():
        reviewed = [json.loads(line) for line in _complete_lines(review_file)]
        original_by_id = {row["id"]: row for row in original_records}
        if len(reviewed) == len(original_records) and {row["id"] for row in reviewed} == set(original_by_id) and all(
            (row["outcome"], row["input_tokens"], row["output_tokens"], row["cached_input_tokens"])
            == (original_by_id[row["id"]]["outcome"], original_by_id[row["id"]]["input_tokens"],
                original_by_id[row["id"]]["output_tokens"], original_by_id[row["id"]]["cached_input_tokens"])
            for row in reviewed
        ):
            records = reviewed
    task_specs = {task["id"]: task for task in spec["tasks"]}
    repetitions = int(spec["trials"])
    mode_list = MODES if any(task.get("split") in {"dev", "test"} for task in task_specs.values()) else ("no-memory",)
    by_case = defaultdict(list)
    for record in records:
        if record.get("task_id") not in task_specs or record.get("mode") not in mode_list:
            raise ValueError("Recorded trial does not belong to its frozen experiment")
        by_case[record["task_id"]].append(record)
    labels = _strict_labels({case_id: by_case[case_id] for case_id in task_specs}, repetitions)
    planned = len(task_specs) * len(mode_list) * repetitions
    trials = []
    for record in records:
        cost, cost_method = _recorded_cost(record, directory)
        reads = record.get("evidence_reads") or []
        hunks = record.get("hunks") or []
        decisions = record.get("evidence_decisions") or []
        timing = record.get("timing") or {}
        verification = (record.get("verifications") or [None])[-1]
        verification = verification or {}
        trials.append({
            "data_origin": spec.get("data_kind", "recorded"),
            "id": record["id"], "task_id": record["task_id"], "mode": record["mode"],
            "trial": record["trial"], "outcome": record["outcome"], "stop_reason": record.get("stop_reason", ""),
            "wall_seconds": timing.get("wall_seconds", 0),
            "ci_wait_seconds": timing.get("ci_wait_seconds", 0),
            "active_seconds": max(0, timing.get("wall_seconds", 0) - timing.get("ci_wait_seconds", 0) - timing.get("approval_wait_seconds", 0)),
            "model_calls": record.get("model_calls", 0), "patch_attempts": record.get("patch_attempts", 0),
            "input_tokens": record.get("input_tokens", 0), "cached_input_tokens": record.get("cached_input_tokens", 0),
            "uncached_input_tokens": record.get("input_tokens", 0) - record.get("cached_input_tokens", 0),
            "output_tokens": record.get("output_tokens", 0), "reasoning_tokens": record.get("reasoning_tokens", 0),
            "billable_tokens": record.get("input_tokens", 0) + record.get("output_tokens", 0),
            "cost_usd": cost, "cost_method": cost_method,
            "history_requests": len(reads), "history_deliveries": sum(r.get("status") == "delivered" for r in reads),
            "raw_deliveries": sum(r.get("status") == "delivered" and r.get("layer") == "raw" for r in reads),
            "reviewed_adopted_effects": sum(d.get("material_effect") in {"change", "refinement"} and d.get("adoption") == "adopted" for d in decisions),
            "evidence_bursts": len(decisions),
            "reviewed_evidence_bursts": sum(d.get("material_effect") not in {None, "unreviewed"} and d.get("adoption") not in {None, "unreviewed"} for d in decisions),
            "unnecessary_hunks": sum(h.get("classification") == "unnecessary" for h in hunks),
            "unreviewed_hunks": sum(h.get("classification") in {None, "unreviewed"} for h in hunks),
            "total_hunks": len(hunks), "packet_insight_ids": record.get("packet_insight_ids") or [],
            "ci_url": _safe_github_url(verification.get("run_url")),
            "oracle_url": _safe_github_url(verification.get("oracle_run_url")),
            "trajectory_path": record.get("trajectory_path"),
            "trajectory_available": (
                isinstance(record.get("trajectory_path"), str)
                and (directory / record["trajectory_path"]).resolve().is_relative_to(directory.resolve())
                and (directory / record["trajectory_path"]).is_file()
            ),
        })
    trials.sort(key=lambda row: (row["task_id"], row["trial"], MODES.index(row["mode"])))
    modes = {}
    for mode in mode_list:
        group = [row for row in trials if row["mode"] == mode]
        outcomes = Counter(row["outcome"] for row in group)
        success = [row for row in group if row["outcome"] == "solved"]
        gain = [row for row in group if labels[row["task_id"]] == "baseline-unsolved"]
        regression = [row for row in group if labels[row["task_id"]] == "baseline-solved"]
        modes[mode] = {
            "label": MODE_LABELS[mode], "planned": len(task_specs) * repetitions,
            "completed": len(group), "outcomes": dict(outcomes),
            "success": _metric(len(success), len(group)),
            "gain": _metric(sum(row["outcome"] == "solved" for row in gain), len(gain)) if mode != "no-memory" else None,
            "regression": _metric(sum(row["outcome"] != "solved" for row in regression), len(regression)) if mode != "no-memory" else None,
            "unnecessary_hunks": sum(row["unnecessary_hunks"] for row in group),
            "unreviewed_hunks": sum(row["unreviewed_hunks"] for row in group),
            "total_hunks": sum(row["total_hunks"] for row in group),
            "median_success_wall_seconds": round(median(row["wall_seconds"] for row in success), 2) if success else None,
            "median_success_active_seconds": round(median(row["active_seconds"] for row in success), 2) if success else None,
            "history_requests": sum(row["history_requests"] for row in group),
            "history_deliveries": sum(row["history_deliveries"] for row in group),
            "raw_deliveries": sum(row["raw_deliveries"] for row in group),
            "reviewed_adopted_effects": sum(row["reviewed_adopted_effects"] for row in group),
            "evidence_bursts": sum(row["evidence_bursts"] for row in group),
            "reviewed_evidence_bursts": sum(row["reviewed_evidence_bursts"] for row in group),
            "model_calls": sum(row["model_calls"] for row in group),
            "patch_attempts": sum(row["patch_attempts"] for row in group),
            "tokens": {key: sum(row[key] for row in group) for key in (
                "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_tokens", "billable_tokens")},
            "cost_usd": round(sum(row["cost_usd"] or 0 for row in group), 6),
            "unknown_cost_trials": sum(row["cost_usd"] is None for row in group),
        }
    cases = []
    for case_id, task in sorted(task_specs.items()):
        group = by_case[case_id]
        metadata = _case_metadata(task)
        cells = {mode: [next((r["outcome"] for r in group if r["mode"] == mode and r["trial"] == i), "pending")
                        for i in range(repetitions)] for mode in mode_list}
        cases.append({"id": case_id, "family": task.get("family"), "label": labels[case_id],
                      **metadata, "cells": cells, "completed": len(group),
                      "planned": len(mode_list) * repetitions,
                      "cost_usd": round(sum((t["cost_usd"] or 0) for t in trials if t["task_id"] == case_id), 6)})
    pairs = {}
    if "wikitrace" in mode_list:
        index = {(row["task_id"], row["trial"], row["mode"]): row for row in trials}
        for baseline in ("no-memory", "insights-only"):
            counts = Counter()
            for case_id in task_specs:
                for i in range(repetitions):
                    c = index.get((case_id, i, "wikitrace"))
                    b = index.get((case_id, i, baseline))
                    if c is None or b is None:
                        continue
                    if c["outcome"] not in {"solved", "unsolved"} or b["outcome"] not in {"solved", "unsolved"}:
                        counts["infrastructure_affected"] += 1
                    elif c["outcome"] == "solved" and b["outcome"] == "unsolved":
                        counts["wikitrace_win"] += 1
                    elif c["outcome"] == "unsolved" and b["outcome"] == "solved":
                        counts["wikitrace_loss"] += 1
                    else:
                        counts["tie"] += 1
            pairs[baseline] = dict(counts)
    started = len([p for p in (directory / "trials").iterdir() if p.is_dir()]) if (directory / "trials").is_dir() else 0
    if spec.get("data_kind") in {"synthetic-demo", "generated-example"}:
        started = len(trials)
    result_file = directory / "results.jsonl"
    return {
        "slug": slug, "id": spec["id"], "model": spec["model"]["model"],
        "classification": _classification(spec, slug),
        "data_origin": spec.get("data_kind", "recorded"),
        "validation": spec.get("validation"),
        "calibration": spec.get("calibration"),
        "planned_cases": len(task_specs), "repetitions": repetitions, "planned_trials": planned,
        "started_trials": started, "completed_trials": len(trials),
        "progress": _metric(len(trials), planned), "modes": modes, "cases": cases,
        "trials": trials, "pairs": pairs, "pricing": PRICE,
        "total_cost_usd": round(sum(row["cost_usd"] or 0 for row in trials), 6),
        "unknown_cost_trials": sum(row["cost_usd"] is None for row in trials),
        "total_tokens": sum(row["billable_tokens"] for row in trials),
        "updated_at": datetime.fromtimestamp(result_file.stat().st_mtime if result_file.is_file() else (directory / "experiment.json").stat().st_mtime,
                                              tz=timezone.utc).isoformat(),
        "source_files": {"experiment": str(directory / "experiment.json"),
                         "results": str(result_file), "manifest": str(directory / "input-manifest.json"),
                         "reviewed_results": str(review_file) if records is not original_records else None},
    }
