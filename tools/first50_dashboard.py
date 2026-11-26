"""Read-only live dashboard for the frozen first-50 exploratory batches.

This operator UI is outside the solver's frozen runtime closure. It reads saved
records and trajectories; it never starts agents or edits experimental inputs.
"""

from __future__ import annotations

import argparse
from collections import Counter
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from buildtrace.dashboard import _recorded_cost
from buildtrace.experiments.models import AgentMode, ExperimentSpec
from buildtrace.experiments.runner import trial_id


REPOSITORY = Path(__file__).resolve().parents[1]
PLAN_ROOT = Path(os.environ.get("BUILDTRACE_FIRST50_PLAN_ROOT", REPOSITORY / "output" / "exploratory-first50-v1"))
RUN_ROOT = Path(os.environ.get("BUILDTRACE_FIRST50_RUN_ROOT", REPOSITORY / "data" / "exploratory-first50" / "experiments"))
MODES = ("no-memory", "insights-only", "wikitrace")
LABELS = {"no-memory": "No memory", "insights-only": "Insights only", "wikitrace": "WikiTrace"}


def _lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    data = path.read_bytes()
    if not data.endswith(b"\n"):
        data = data.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in data else b""
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line]


def _proof_status(record: dict) -> str:
    if record.get("mode") != "wikitrace":
        return "No history tool"
    reads = record.get("evidence_reads") or []
    if any(read.get("layer") == "raw" and read.get("status") == "delivered"
           and int(read.get("delivered_characters") or 0) > 0 for read in reads):
        return "Original proof opened"
    return "History requested; no raw proof" if reads else "No history request"


def _trace_details(directory: Path, record: dict) -> tuple[list[dict], dict[str, str]]:
    relative = record.get("trajectory_path")
    if not isinstance(relative, str):
        return [], {}
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        return [], {}
    plans, excerpts = [], {}
    for event in _lines(path):
        if event.get("type") != "function_call":
            continue
        if event.get("name") == "set_plan":
            plans.append(event.get("arguments", {}))
        elif event.get("name") == "read_historical_evidence":
            result = event.get("result") or {}
            if isinstance(result, dict) and isinstance(result.get("read_id"), str) and isinstance(result.get("text"), str):
                excerpts[result["read_id"]] = result["text"]
    return plans, excerpts


def load() -> dict:
    plan = json.loads((PLAN_ROOT / "batch-plan.json").read_text(encoding="utf-8"))
    batches = []
    for item in plan["batches"]:
        number = item["number"]
        directory = RUN_ROOT / f"batch-{number:02d}"
        records = _lines(directory / "results.jsonl")
        packets_file = directory / "packets.json"
        packets = json.loads(packets_file.read_text(encoding="utf-8")) if packets_file.is_file() else {}
        active = []
        spec_file = directory / "experiment.json"
        if spec_file.is_file():
            spec = ExperimentSpec.model_validate_json(spec_file.read_text(encoding="utf-8"))
            identities = {trial_id(spec, task, mode, trial): (task.id, mode.value, trial + 1)
                          for task in spec.tasks for mode in AgentMode
                          for trial in range(spec.trials)}
            for trial_dir in (directory / "trials").glob("run-*"):
                if not trial_dir.is_dir() or (trial_dir / "result.json").exists() or trial_dir.name not in identities:
                    continue
                events = _lines(trial_dir / "trajectory.jsonl")
                calls = [event for event in events if event.get("type") == "function_call"]
                task_id, mode, trial = identities[trial_dir.name]
                active.append({"id": trial_dir.name, "task_id": task_id, "mode": mode, "trial": trial,
                               "model_calls": sum(event.get("type") == "message" and bool(event.get("usage")) for event in events),
                               "last_action": calls[-1].get("name") if calls else "starting"})
        active.sort(key=lambda row: (row["task_id"], row["trial"], MODES.index(row["mode"])))
        attempts = []
        for record in records:
            cost, method = _recorded_cost(record, directory)
            reads = record.get("evidence_reads") or []
            checks = record.get("verifications") or []
            last_check = checks[-1] if checks else {}
            plans, excerpts = _trace_details(directory, record)
            attempts.append({
                "id": record["id"], "task_id": record["task_id"],
                "mode": record["mode"], "trial": record["trial"] + 1,
                "outcome": record["outcome"], "stop_reason": record.get("stop_reason", ""),
                "proof_status": _proof_status(record), "history_requests": len(reads),
                "raw_deliveries": sum(r.get("layer") == "raw" and r.get("status") == "delivered"
                                      and int(r.get("delivered_characters") or 0) > 0 for r in reads),
                "reads": reads, "packet_insight_ids": record.get("packet_insight_ids") or [],
                "plans": plans, "excerpts": excerpts,
                "input_tokens": record.get("input_tokens", 0),
                "cached_input_tokens": record.get("cached_input_tokens", 0),
                "output_tokens": record.get("output_tokens", 0),
                "reasoning_tokens": record.get("reasoning_tokens", 0),
                "cost_usd": cost, "cost_method": method,
                "model_calls": record.get("model_calls", 0),
                "ci_status": last_check.get("workflow_conclusion") or "not run",
                "oracle_status": "passed" if last_check.get("oracle_passed") is True else "failed" if last_check.get("oracle_passed") is False else "not run",
                "oracle_details": last_check.get("details") or "",
                "ci_url": last_check.get("run_url"),
                "oracle_url": last_check.get("oracle_run_url"),
                "reviewed_adopted_effects": sum(
                    d.get("material_effect") in {"change", "refinement"} and d.get("adoption") == "adopted"
                    for d in record.get("evidence_decisions") or []),
                "trace_available": bool(record.get("trajectory_path") and
                                        (directory / record["trajectory_path"]).is_file()),
            })
        attempts.sort(key=lambda row: (row["task_id"], row["trial"], MODES.index(row["mode"])))
        batches.append({"number": number, "problem_ids": item["problem_ids"],
                        "domains": item["domains"], "attempts": attempts, "active": active,
                        "packets": packets,
                        "completed": len(attempts), "planned": len(item["problem_ids"]) * 3 * 3,
                        "directory": directory})
    return {"plan": plan, "batches": batches}


def _tag(value: str, label: str | None = None) -> str:
    css = re.sub(r"[^a-z0-9-]", "-", value.lower())
    return f'<span class="tag {css}">{escape(label or value)}</span>'


def _link(url: str | None, label: str) -> str:
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        return ""
    return f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener">{escape(label)}</a>'


def render(data: dict, batch_number: int = 1) -> str:
    batch = data["batches"][batch_number - 1]
    attempts = batch["attempts"]
    counts = Counter(row["outcome"] for row in attempts)
    modes = {mode: [row for row in attempts if row["mode"] == mode] for mode in MODES}
    cost = sum(row["cost_usd"] or 0 for row in attempts)
    unknown = sum(row["cost_usd"] is None for row in attempts)
    proof = sum(row["proof_status"] == "Original proof opened" for row in modes["wikitrace"])
    baseline_by_task = {task: [row for row in modes["no-memory"] if row["task_id"] == task]
                        for task in batch["problem_ids"]}
    baseline_labels = {}
    for task, rows in baseline_by_task.items():
        if len(rows) == 3 and all(row["outcome"] == "solved" for row in rows):
            baseline_labels[task] = "baseline-solved"
        elif len(rows) == 3 and all(row["outcome"] == "unsolved" for row in rows):
            baseline_labels[task] = "baseline-unsolved"
    pair_rows = []
    for mode in ("insights-only", "wikitrace"):
        gain_rows = [row for row in modes[mode] if baseline_labels.get(row["task_id"]) == "baseline-unsolved"]
        regression_rows = [row for row in modes[mode] if baseline_labels.get(row["task_id"]) == "baseline-solved"]
        gain = sum(row["outcome"] == "solved" for row in gain_rows)
        regression = sum(row["outcome"] == "unsolved" for row in regression_rows)
        pair_rows.append(f'<div><b>{escape(LABELS[mode])}</b><span>Gain {gain}/{len(gain_rows)} · Regression {regression}/{len(regression_rows)}</span></div>')
    nav = "".join(f'<a class="{"active" if b["number"] == batch_number else ""}" href="/?batch={b["number"]}">Batch {b["number"]} <small>{b["completed"]}/{b["planned"]}</small></a>' for b in data["batches"])
    cards = "".join(
        f'<article><span>{escape(LABELS[mode])}</span><strong>{sum(r["outcome"] == "solved" for r in rows)}/{len(rows)}</strong><small>verified repairs / completed attempts</small></article>'
        for mode, rows in modes.items()
    )
    active_rows = "".join(
        f'<tr><td><b>{escape(row["task_id"])}</b><small>Try {row["trial"]}</small></td><td>{escape(LABELS[row["mode"]])}</td>'
        f'<td>{row["model_calls"]}</td><td>{escape(row["last_action"])}</td>'
        f'<td><a href="/trace/{escape(row["id"], quote=True)}?batch={batch_number}">Open live trace</a></td></tr>'
        for row in batch["active"]
    )
    active_section = (f'<section><h2>Agents running now · {len(batch["active"])}</h2><div class="table-wrap"><table>'
                      f'<thead><tr><th>Problem</th><th>Agent</th><th>Model calls</th><th>Latest tool action</th><th>Trace</th></tr></thead>'
                      f'<tbody>{active_rows}</tbody></table></div></section>') if batch["active"] else ""
    case_rows = []
    for i, task in enumerate(batch["problem_ids"], 1):
        packet = batch["packets"].get(task)
        insight = (f'<details><summary>{len(packet.get("insight_ids", []))} selected insights</summary>'
                   f'<pre>{escape(packet.get("text", ""))}</pre></details>') if packet else "Pending selection"
        case_rows.append(f'<tr><td>{i}</td><td><b>{escape(task)}</b></td><td>{escape(batch["domains"][i-1])}</td><td>{sum(row["task_id"] == task for row in attempts)}/9</td><td>{insight}</td></tr>')
    cases = "".join(case_rows)
    trial_rows = []
    for row in attempts:
        proof_class = "opened" if row["proof_status"] == "Original proof opened" else "requested" if row["history_requests"] else "none"
        evidence_parts = []
        for read in row["reads"]:
            excerpt = row["excerpts"].get(read.get("id"))
            proof_view = f'<details><summary>View delivered excerpt</summary><pre>{escape(excerpt)}</pre></details>' if excerpt else ""
            evidence_parts.append(
                f'<li><b>{escape(str(read.get("insight_id", "")))}</b> · {escape(str(read.get("layer", "")))} · {escape(str(read.get("status", "")))} · '
                f'{escape(str(read.get("document", "")))} · {int(read.get("delivered_characters") or 0)} characters {proof_view}</li>'
            )
        evidence = "".join(evidence_parts) or "<li>No history request recorded.</li>"
        plans = "".join(f'<li>{escape(str(plan.get("summary", "")))}</li>' for plan in row["plans"]) or "<li>No saved plan note.</li>"
        sources = _link(row["ci_url"], "CI") + " " + _link(row["oracle_url"], "oracle")
        trace = f'<a href="/trace/{escape(row["id"], quote=True)}?batch={batch_number}">full observable trace</a>' if row["trace_available"] else ""
        trial_rows.append(
            f'<tr><td><b>{escape(row["task_id"])}</b><small>Try {row["trial"]}</small></td>'
            f'<td>{escape(LABELS[row["mode"]])}</td><td>{_tag(row["outcome"])}</td>'
            f'<td>CI {escape(row["ci_status"])}<small>Oracle {escape(row["oracle_status"])}</small></td>'
            f'<td><span class="proof {proof_class}">{escape(row["proof_status"])}</span><small>{row["history_requests"]} requests · {row["raw_deliveries"]} raw</small></td>'
            f'<td>{row["input_tokens"]:,} in · {row["output_tokens"]:,} out</td>'
            f'<td>{"unknown" if row["cost_usd"] is None else "$"+format(row["cost_usd"], ".5f")}</td>'
            f'<td><details><summary>Inspect</summary><p><b>Selected insights:</b> {escape(", ".join(row["packet_insight_ids"]) or "none")}</p>'
            f'<p><b>Evidence requests</b></p><ul>{evidence}</ul><p><b>Plan notes</b></p><ol>{plans}</ol>'
            f'<p><b>Stop reason:</b> {escape(str(row["stop_reason"]))}</p><p><b>Oracle details:</b> {escape(row["oracle_details"])}</p><p>{sources} {trace}</p>'
            f'<p><b>Independently reviewed, adopted evidence effects:</b> {row["reviewed_adopted_effects"]}</p>'
            f'</details></td></tr>'
        )
    rows_html = "".join(trial_rows) or '<tr><td colspan="8" class="empty">No agent attempts have run in this batch.</td></tr>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>First 50 · Batch {batch_number} · CI/CD memory lab</title><style>
:root{{--bg:#0d1420;--panel:#182333;--line:#314358;--text:#edf5ff;--muted:#a9bed2;--cyan:#64e3e1;--green:#6ce1bd;--red:#ff9fa8}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,sans-serif}}main{{max-width:1420px;margin:auto;padding:32px 22px 90px}}h1{{font-size:2rem;margin:.2rem 0}}h2{{font-size:1.25rem;margin:0 0 14px}}p{{color:var(--muted)}}a{{color:var(--cyan)}}.eyebrow{{color:var(--cyan);text-transform:uppercase;letter-spacing:.12em;font-size:.75rem}}.meta{{display:flex;justify-content:space-between;gap:20px;align-items:end;flex-wrap:wrap}}.meta p{{max-width:850px}}.button{{border:1px solid var(--line);border-radius:8px;padding:9px 15px;text-decoration:none}}nav{{display:flex;gap:8px;flex-wrap:wrap;margin:24px 0}}nav a{{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:9px 13px;text-decoration:none}}nav .active{{border-color:var(--cyan)}}nav small{{color:var(--muted);margin-left:4px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}article,section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}}article span,article small{{display:block;color:var(--muted)}}article strong{{display:block;font-size:2rem}}section{{margin-top:14px}}.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}.summary b{{display:block;font-size:1.25rem}}.summary span{{color:var(--muted)}}.compare{{display:flex;gap:25px;flex-wrap:wrap}}.compare div{{display:flex;flex-direction:column}}.compare span{{color:var(--muted)}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:10px 9px;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--muted);font-size:.82rem}}td small{{display:block;color:var(--muted)}}.tag,.proof{{display:inline-block;border-radius:6px;padding:2px 7px;background:#24354c}}.solved,.opened{{background:#164c45;color:var(--green)}}.unsolved,.infra-error{{background:#51313e;color:var(--red)}}.requested{{color:#ffd78d}}.none{{color:var(--muted)}}details{{min-width:110px}}details[open]{{min-width:310px}}summary{{cursor:pointer;color:var(--cyan)}}details p,details li{{font-size:.86rem;overflow-wrap:anywhere}}.empty{{text-align:center;color:var(--muted);padding:30px}}.note{{font-size:.88rem}}@media(max-width:700px){{main{{padding:18px 12px}}h1{{font-size:1.5rem}}}}
pre{{white-space:pre-wrap;word-break:break-word;max-height:400px;overflow:auto;background:#0e1826;border:1px solid var(--line);border-radius:8px;padding:10px}}
</style></head><body><main><div class="meta"><div><div class="eyebrow">Exploratory CI/CD agent memory study</div><h1>First 50 · batch {batch_number}</h1><p>Ten previously unseen problems, three agent modes, three attempts each. The learning corpus and scoring rules stay frozen. These exploratory batches do not establish a formal held-out verdict.</p></div><a class="button" href="/?batch={batch_number}">Refresh records</a></div>
<nav>{nav}</nav><div class="cards">{cards}</div><section class="summary"><div><b>{batch["completed"]}/{batch["planned"]}</b><span>attempts recorded</span></div><div><b>{len(batch["active"])}</b><span>agents running now</span></div><div><b>{counts["solved"]}</b><span>verified repairs</span></div><div><b>{proof}/{len(modes["wikitrace"])}</b><span>WikiTrace attempts that opened original proof</span></div><div><b>${cost:.5f}</b><span>estimated solver token cost{f" · {unknown} unknown" if unknown else ""}</span></div></section>{active_section}
<section><h2>Gain and regression</h2><div class="compare">{''.join(pair_rows)}</div><p class="note">Gain counts repairs on problems all three No-memory attempts failed. Regression counts failures on problems all three No-memory attempts solved. Mixed or incomplete baselines stay out of both denominators. A raw evidence read alone does not prove it changed a repair; independent source-to-action review is separate.</p><p class="note">Planning estimate for 90 attempts: about $0.29 in solver text tokens, based on the six-problem development average. Actual recorded usage above replaces this estimate as trials finish. GitHub Actions compute and taxes are separate.</p></section>
<section><h2>Problems and selected insights</h2><p class="note">The same frozen insight packet is passed to Insights only and WikiTrace. No memory receives no packet. Open a packet to see its actual text and source steps.</p><div class="table-wrap"><table><thead><tr><th>No.</th><th>Problem</th><th>Area</th><th>Recorded</th><th>Insight packet</th></tr></thead><tbody>{cases}</tbody></table></div></section>
<section><h2>Every attempt and evidence link</h2><div class="table-wrap"><table><thead><tr><th>Problem</th><th>Agent</th><th>Outcome</th><th>CI / oracle</th><th>Original proof</th><th>Tokens</th><th>Cost estimate</th><th>Source, plans, CI</th></tr></thead><tbody>{rows_html}</tbody></table></div><p class="note">A public CI success is not a verified repair when the separate oracle fails. Open Inspect for the verifier details, source excerpts, plan notes, and CI links. The full observable trace does not expose private model chain of thought. Token cost uses the saved GPT-6 Luna price table and is an estimate, not an invoice. GitHub Actions compute is separate.</p></section>
<p class="note">Frozen plan SHA-256: {escape(data["plan"]["sha256"])} · 50 selected problems in five batches · 100 formal test problems remain untouched.</p></main></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlsplit(self.path)
        params = dict(part.split("=", 1) for part in path.query.split("&") if "=" in part)
        try:
            number = int(params.get("batch", "1"))
            if number not in range(1, 6):
                raise ValueError
            data = load()
        except (ValueError, OSError, KeyError):
            self.send_error(400, "Invalid batch or missing frozen plan")
            return
        if path.path == "/":
            payload = render(data, number).encode("utf-8")
            kind = "text/html; charset=utf-8"
        elif path.path == "/api":
            batch = data["batches"][number - 1]
            payload = json.dumps({k: v for k, v in batch.items() if k != "directory"}, default=str).encode("utf-8")
            kind = "application/json; charset=utf-8"
        elif re.fullmatch(r"/trace/run-[a-z0-9]+", path.path):
            identifier = unquote(path.path.removeprefix("/trace/"))
            batch = data["batches"][number - 1]
            if not (any(row["id"] == identifier and row["trace_available"] for row in batch["attempts"])
                    or any(row["id"] == identifier for row in batch["active"])):
                self.send_error(404)
                return
            trace = (batch["directory"] / "trials" / identifier / "trajectory.jsonl").resolve()
            if not trace.is_relative_to(batch["directory"].resolve()) or not trace.is_file():
                self.send_error(404)
                return
            payload = trace.read_bytes()
            kind = "application/x-ndjson; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    global PLAN_ROOT, RUN_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--snapshot", type=Path, help="Write a static HTML snapshot instead of serving")
    parser.add_argument("--plan-root", type=Path, default=PLAN_ROOT)
    parser.add_argument("--run-root", type=Path, default=RUN_ROOT)
    args = parser.parse_args()
    PLAN_ROOT, RUN_ROOT = args.plan_root.resolve(), args.run_root.resolve()
    if args.snapshot:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(render(load()), encoding="utf-8")
        print(args.snapshot.resolve())
    else:
        print(f"Read-only first-50 dashboard: http://127.0.0.1:{args.port}/", flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
