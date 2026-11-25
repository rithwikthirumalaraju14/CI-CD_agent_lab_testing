"""Run the 50-problem, three-mode local repair study with live model calls."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import random
import re
import time

from dotenv import load_dotenv
import httpx
from wikitrace.memory.insights import render_inject

from buildtrace.experiments.memory import FrozenMemory
from buildtrace.experiments.models import ModelSettings
from buildtrace.experiments.openai import OpenAIClient, OpenAIError
from local_repair_cases import evaluate, fixtures, verify


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/local-50/experiments/dev-v1"
CORPUS = ROOT / "data/research-v2/memory/train-v1"
MODES = ("no-memory", "insights-only", "wikitrace")
REPEATS = 3
SETTINGS = ModelSettings(model="gpt-6-luna", temperature=None, reasoning_effort="low")
SYSTEM = (
    "Repair a bounded Python expression using one of the four candidate replacements. "
    "Honor the current contract and failing example. Historical insights, when supplied, "
    "are observations from other problems. Return only the letter A, B, C, or D."
)
READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read_history",
        "description": "Read one original source excerpt linked to an assigned historical insight.",
        "parameters": {
            "type": "object",
            "properties": {
                "insight_id": {"type": "string"},
                "layer": {"type": "string", "enum": ["compilation", "raw"]},
            },
            "required": ["insight_id", "layer"],
        },
    },
}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def prepare():
    cases = list(fixtures())
    for case in cases:
        passing = [option for option in case["options"] if verify(case, option)]
        if len(passing) != 1 or case["options"][passing[0]] == case["original"]:
            raise ValueError(f"Expected one passing repair and a failing original: {case['id']} ({passing})")
    spec = {
        "id": "50 development v1",
        "data_kind": "live",
        "validation": "local-expression",
        "trials": REPEATS,
        "model": SETTINGS.model_dump(mode="json"),
        "tasks": [{key: case[key] for key in ("id", "family", "split")} for case in cases],
        "fixtures_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "corpus_manifest_sha256": hashlib.sha256((CORPUS / "corpus.json").read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "experiment.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != spec:
        raise ValueError("Existing study inputs differ from this runner")
    if not path.exists():
        write_json(path, spec)
        write_json(OUT / "local-checks.json", cases)
    (OUT / "results.jsonl").touch(exist_ok=True)
    return cases


def prompt(case):
    for x, k, expected in case["checks"]:
        actual = evaluate(case["original"], x, k)
        if actual != expected or type(actual) is not type(expected):
            break
    return (
        f"{case['contract']}\nCurrent expression: {case['original']}\n"
        f"Failing example: x={x!r}, k={k}; expected {expected!r}, got {actual!r}.\n"
        + "\n".join(f"{letter}: {expression}" for letter, expression in case["options"].items())
    )


def packet(case, memory):
    terms = set(re.findall(r"[a-z_]{3,}", prompt(case).lower()))

    def score(item):
        prose = item.title + " " + " ".join(body for _, body in item.sections)
        matches = terms.intersection(re.findall(r"[a-z_]{3,}", prose.lower()))
        return (-len(matches), item.id)

    items = sorted(memory.items, key=score)[:2]
    return render_inject(items, source=True), [item.id for item in items]


def trial(case, mode, attempt, model, memory, assigned):
    key = f"{case['id']}-{mode}-{attempt + 1}"
    directory = OUT / "trials" / key
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    messages = [{"role": "system", "content": SYSTEM}]
    insights, ids = assigned if mode != "no-memory" else ("", [])
    question = prompt(case)
    if insights:
        question += "\n\nHistorical insights:\n" + insights
    messages.append({"role": "user", "content": question})
    evidence = []
    turns = []
    choice = ""
    calls = 0
    error = None
    try:
        calls += 1
        turn = model.complete(messages, max_tokens=384, tools=[READ_TOOL] if mode == "wikitrace" else None, timeout=25)
        turns.append(turn)
        requested = turn.message.get("tool_calls", [])
        if mode == "wikitrace" and len(requested) == 1 and requested[0]["function"]["name"] == "read_history":
            call = requested[0]
            layer = ""
            try:
                args = json.loads(call["function"]["arguments"])
                layer = args["layer"]
                excerpt, _ = memory.read(args["insight_id"], layer, limit=1400, allowed_insight_ids=ids)
                evidence.append({"layer": layer, "status": "delivered", "citation": excerpt["citation"]})
                reply = json.dumps(excerpt)
            except (KeyError, TypeError, ValueError):
                evidence.append({"layer": str(layer), "status": "denied"})
                reply = "That historical excerpt is unavailable."
            calls += 1
            turn = model.complete(
                messages + turn.continuation_items + [
                    {"type": "function_call_output", "call_id": call["id"], "output": reply},
                ], max_tokens=384, timeout=25,
            )
            turns.append(turn)
        choice = (turn.message.get("content") or "").strip().upper()
        solved = not turn.stop_reason and verify(case, choice)
        reason = "local-checks-passed" if solved else (turn.stop_reason or "local-checks-failed-or-invalid-choice")
    except OpenAIError as exc:
        solved = False
        error = str(exc)
        reason = error
    record = {
        "id": key, "task_id": case["id"], "mode": mode, "trial": attempt, "role": "reference",
        "outcome": "infra-error" if error else "solved" if solved else "unsolved",
        "stop_reason": reason, "choice": choice, "model": SETTINGS.model_dump(mode="json"),
        "returned_models": sorted({turn.returned_model for turn in turns}), "model_calls": calls,
        "patch_attempts": int(choice in case["options"]),
        "timing": {"wall_seconds": round(time.monotonic() - started, 4), "ci_wait_seconds": 0},
        "evidence_reads": evidence, "packet_insight_ids": ids,
    }
    for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
        record[field] = sum(getattr(turn, field) for turn in turns)
    write_json(directory / "result.json", record)
    return record


def run(cases, workers):
    print("Checking the frozen WikiTrace memory corpus.", flush=True)
    memory = FrozenMemory(CORPUS)
    packets = {case["id"]: packet(case, memory) for case in cases}
    ledger = OUT / "results.jsonl"
    completed = {row["id"]: row for line in ledger.read_text(encoding="utf-8").splitlines()
                 if line and (row := json.loads(line))}
    jobs = [(case, mode, attempt) for case in cases for mode in MODES for attempt in range(REPEATS)
            if f"{case['id']}-{mode}-{attempt + 1}" not in completed]
    random.Random(50).shuffle(jobs)
    counts = Counter(row["outcome"] for row in completed.values())

    def save(record, stream):
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        counts[record["outcome"]] += 1

    with httpx.Client() as transport, ledger.open("a", encoding="utf-8") as stream:
        model = OpenAIClient(SETTINGS, client=transport)
        print(f"Starting {len(jobs)} remaining model trials with {workers} workers.", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(jobs), workers):
                chunk = jobs[start:start + workers]
                futures = [pool.submit(trial, case, mode, attempt, model, memory, packets[case["id"]])
                           for case, mode, attempt in chunk]
                rows = []
                for future in as_completed(futures):
                    row = future.result()
                    save(row, stream)
                    rows.append(row)
                print(f"{sum(counts.values())}/450 · {dict(counts)}", flush=True)
                if all(row["outcome"] == "infra-error" for row in rows):
                    raise SystemExit(f"Paused after a batch of infrastructure errors: {rows[0]['stop_reason']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Save and validate the study inputs without API calls")
    parser.add_argument("--workers", type=int, choices=range(1, 25), default=12)
    args = parser.parse_args()
    cases = prepare()
    print(f"50 development v1: {len(cases)} local problems, {len(cases) * len(MODES) * REPEATS} planned trials.", flush=True)
    if not args.prepare:
        load_dotenv(ROOT / ".env")
        run(cases, args.workers)


if __name__ == "__main__":
    main()
