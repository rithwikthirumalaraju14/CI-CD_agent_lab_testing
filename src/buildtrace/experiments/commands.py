"""CLI wiring for training, frozen memory, paired experiments, and all six measurements."""

import json
import os
import sys
from pathlib import Path

from buildtrace.evidence import EvidenceError, EvidenceStore
from buildtrace.experiments.backend import BackendError
from buildtrace.experiments.memory import FrozenMemory, build_memory
from buildtrace.experiments.metrics import build_report, read_records, render_markdown
from buildtrace.experiments.openai import OpenAIClient, OpenAIError
from buildtrace.experiments.workflow import (
    RESEARCH_RUNTIME_FILES, apply_research_reviews, apply_reviews, load_spec, review_template,
    run_experiment, validate_local_inputs,
)
from buildtrace.experiments.models import fingerprint
from buildtrace.github import GitHubActionsClient, GitHubError
from buildtrace.models import FAILED_CONCLUSIONS, RunReference


def add_commands(commands):
    experiment = commands.add_parser("experiment", help="Compare no-memory, insights-only, and WikiTrace")
    actions = experiment.add_subparsers(dest="experiment_action", required=True)
    validate = actions.add_parser("validate", help="Check local experiment inputs without model calls or GitHub writes")
    run = actions.add_parser("run", help="Run shared agents on the train, development, or held-out test split")
    memory = actions.add_parser("build-memory", help="Compile actual no-memory training sessions with upstream WikiTrace")
    report = actions.add_parser("report", help="Compute paired metrics and write JSON and Markdown reports")
    review = actions.add_parser("review", help="Attach independent hunk/evidence reviews to a derived results file")
    template = actions.add_parser("review-template", help="Export blinded hunk/evidence annotation forms")
    capture = actions.add_parser("capture-inputs", help="Archive the exact declared failing runs without model calls")
    attach = actions.add_parser("attach-memory", help="Bind verified frozen memory hashes to an evaluation spec")
    manifest = actions.add_parser("manifest", help="Create a content-addressed research phase candidate without live calls")
    gate = actions.add_parser("gate", help="Replay the predeclared development source-to-action mechanism gate")
    freeze = actions.add_parser("freeze-protocol", help="Approve an unchanged protocol after replaying its development gate")
    evaluation = actions.add_parser("freeze-evaluation", help="Bind untouched held-out tasks to the approved research protocol")
    for action in (validate, run, memory, report, capture, attach, manifest, gate, freeze, evaluation):
        action.add_argument("--spec", type=Path, required=True)
    for action in (validate, run, memory, capture):
        action.add_argument("--data-dir", type=Path, default=Path(os.environ.get("BUILDTRACE_DATA_DIR", "data")))
    run.add_argument("--split", choices=["train", "dev", "test"], required=True)
    manifest.add_argument("--split", choices=["train", "dev", "test"], required=True)
    for action in (validate, capture):
        action.add_argument("--split", choices=["train", "dev", "test"])
    report.add_argument("--split", choices=["dev", "test"], default="test")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--allow-publish", action="store_true", help="Authorize trial commits/branches and paid CI/model execution")
    run.add_argument("--include-reruns", action="store_true", help="Also collect independent no-memory reruns on test tasks")
    run.add_argument("--continue-on-infra", action="store_true", help="Continue while retaining recorded infrastructure failures")
    run.add_argument("--max-new-trials", type=int, help="Foreground checkpoint size; preserves the full predeclared grid")
    for action in (memory, report, review, template, gate, freeze):
        action.add_argument("--records", type=Path, required=True)
        action.add_argument("--output", type=Path, required=True)
    report.add_argument("--bootstrap", type=int, default=2000)
    review.add_argument("--annotations", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    attach.add_argument("--corpus", type=Path, required=True)
    attach.add_argument("--output", type=Path, required=True)
    for action in (manifest, evaluation):
        action.add_argument("--output", type=Path, required=True)
    for action in (run, report, gate, freeze, evaluation):
        action.add_argument("--manifest", type=Path, required=action in (gate, freeze, evaluation))
    for action in (run, manifest, freeze, evaluation):
        action.add_argument("--runtime-file", action="append", default=[], help="Additional explicit runtime/control file to freeze")
    for action in (run, manifest):
        action.add_argument("--gate-task", action="append", help="Predeclare a mechanism-applicable development task")
    for action in (gate, freeze):
        action.add_argument("--independent-reviewer", action="append", required=True)
        action.add_argument("--solver-identity", action="append", default=["solver-agent"])
    for action in (run, report, evaluation):
        action.add_argument("--protocol-freeze", type=Path, required=action is evaluation)
    for action in (run, report):
        action.add_argument("--evaluation-freeze", type=Path)
    template.add_argument("--separate", action="store_true", help="Write unlinked v2 hunk/evidence packages and a private mapping")
    template.add_argument("--manifest", type=Path)
    template.add_argument("--corpus", type=Path)
    review.add_argument("--mapping", type=Path, help="Private mapping for separately anonymized v2 reviews")


def _json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path else None


def _save_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def dispatch(args) -> int:
    try:
        if args.experiment_action == "review-template":
            if args.output.exists():
                raise ValueError("Choose a new annotation file")
            records, original = read_records(args.records), args.records.read_bytes()
            if args.separate or any(record.schema_version == 2 for record in records):
                from buildtrace.experiments.research import create_review_packages
                corpus = FrozenMemory(args.corpus) if args.corpus else None
                hunks, evidence, mapping = create_review_packages(records, original, manifest=_json(args.manifest), memory=corpus)
                args.output.mkdir(parents=True, exist_ok=False)
                for name, value in (("hunks.json", hunks), ("evidence.json", evidence), ("private-mapping.json", mapping)):
                    _save_new(args.output / name, value)
                print(f"Saved separate hunk/evidence packages and private mapping to {args.output}")
                return 0
            form = review_template(records, original)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(form, indent=2) + "\n")
            print(f"Saved independent review template to {args.output}")
            return 0
        if args.experiment_action == "review":
            records = read_records(args.records)
            if args.mapping:
                records = apply_research_reviews(records, _json(args.annotations), _json(args.mapping), args.records.read_bytes())
            else:
                if any(record.schema_version == 2 for record in records):
                    raise ValueError("Use the separate research review mapping for v2 records")
                records = apply_reviews(records, args.annotations, args.records.read_bytes())
            if args.output.exists():
                raise ValueError("Choose a new derived results file; original records are never overwritten")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write("".join(record.model_dump_json() + "\n" for record in records))
            print(json.dumps({"reviewed_records": len(records), "output": str(args.output)}, indent=2))
            return 0
        spec = load_spec(args.spec)
        if args.experiment_action in {"manifest", "gate", "freeze-protocol", "freeze-evaluation"}:
            from buildtrace.experiments.research import (
                create_input_manifest, evaluate_development_gate, freeze_evaluation, freeze_protocol,
            )
            if args.output.exists():
                raise ValueError("Choose a new research artifact path")
            corpus = FrozenMemory(spec.corpus, spec.corpus_sha256) if spec.corpus else None
            extras = (*RESEARCH_RUNTIME_FILES, *getattr(args, "runtime_file", []))
            if args.experiment_action == "manifest":
                value = create_input_manifest(spec, args.split, memory=corpus, extra_files=extras, gate_task_ids=args.gate_task)
            elif args.experiment_action == "freeze-evaluation":
                value = freeze_evaluation(spec, manifest=_json(args.manifest), protocol_freeze=_json(args.protocol_freeze), memory=corpus, extra_files=extras)
            else:
                function = evaluate_development_gate if args.experiment_action == "gate" else freeze_protocol
                kwargs = {"manifest": _json(args.manifest), "memory": corpus,
                          "independent_reviewers": args.independent_reviewer, "solver_identities": args.solver_identity}
                if args.experiment_action == "freeze-protocol":
                    kwargs["extra_files"] = extras
                value = function(spec, read_records(args.records), **kwargs)
            _save_new(args.output, value)
            print(json.dumps({key: value[key] for key in ("kind", "sha256", "passed", "failures") if key in value}, indent=2))
            return 0 if value.get("passed", True) else 2
        if args.experiment_action in {"capture-inputs", "attach-memory"}:
            if args.output.exists():
                raise ValueError("Choose a new output specification file")
            if args.experiment_action == "capture-inputs":
                store, tasks = EvidenceStore(args.data_dir), []
                with GitHubActionsClient() as client:
                    for task in spec.tasks:
                        if args.split is not None and task.split != args.split:
                            tasks.append(task)
                            continue
                        collected = client.collect(RunReference.parse(task.initial_run_url))
                        if collected.source_ref != task.base_sha or collected.run.get("conclusion") not in FAILED_CONCLUSIONS or collected.log_archive is None:
                            raise ValueError(f"{task.id}: the exact failing source revision and original logs are required")
                        archived = store.save(collected)
                        tasks.append(task.model_copy(update={"capture_id": archived["id"], "capture_sha256": fingerprint(archived)}))
                spec = spec.model_copy(update={"tasks": tasks})
            else:
                corpus = FrozenMemory(args.corpus)
                corpus.assert_test_separation(spec)
                spec = spec.model_copy(update={"corpus": corpus.directory, "corpus_sha256": corpus.sha256, "insights_sha256": corpus.insights_sha256})
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(spec.model_dump_json(indent=2) + "\n")
            print(json.dumps({"specification": str(args.output), "experiment_sha256": fingerprint(spec)}, indent=2))
        elif args.experiment_action == "validate":
            result = validate_local_inputs(spec, EvidenceStore(args.data_dir), split=args.split)
            print(json.dumps(result, indent=2))
            return 0 if result["local_prerequisites_ready"] else 2
        elif args.experiment_action == "run":
            records = run_experiment(
                spec, EvidenceStore(args.data_dir), args.output, split=args.split,
                allow_publish=args.allow_publish, include_reruns=args.include_reruns,
                continue_on_infra=args.continue_on_infra,
                phase_manifest=_json(args.manifest), evaluation_freeze=_json(args.evaluation_freeze),
                protocol_freeze=_json(args.protocol_freeze), max_new_trials=args.max_new_trials,
                runtime_extra_files=(*RESEARCH_RUNTIME_FILES, *args.runtime_file) if spec.schema_version == 2 else (),
                gate_task_ids=args.gate_task,
            )
            print(json.dumps({"completed_runs": len(records), "results": str(args.output / "results.jsonl"), "frozen_spec": str(args.output / "experiment.json")}, indent=2))
        elif args.experiment_action == "build-memory":
            records = read_records(args.records)
            with OpenAIClient(spec.model) as transport:
                corpus = build_memory(spec, records, args.records.parent.resolve(), EvidenceStore(args.data_dir), args.output, transport)
            print(json.dumps({"corpus": str(args.output.resolve()), "corpus_sha256": corpus.sha256, "insights_sha256": corpus.insights_sha256, "insights": len(corpus.items)}, indent=2))
        elif args.experiment_action == "report":
            result = build_report(
                spec, read_records(args.records), resamples=args.bootstrap, split=args.split,
                phase_manifest=_json(args.manifest), evaluation_freeze=_json(args.evaluation_freeze), protocol_freeze=_json(args.protocol_freeze),
            )
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            (args.output / "summary.md").write_text(render_markdown(result), encoding="utf-8")
            print(render_markdown(result))
            print(f"Saved reports to {args.output}")
        return 0
    except (ValueError, OSError, KeyError, EvidenceError, BackendError, OpenAIError, GitHubError) as exc:
        print(f"buildtrace experiment: {exc}", file=sys.stderr)
        return 2
