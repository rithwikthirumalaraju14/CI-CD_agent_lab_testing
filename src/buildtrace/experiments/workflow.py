"""Freeze inputs, counterbalance the three modes, resume completed trials, and export records."""

import hashlib
import json
import os
import random
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from buildtrace.evidence import EvidenceStore
from buildtrace.experiments.backend import GitHubTrialBackend
from buildtrace.experiments.memory import FrozenMemory
from buildtrace.experiments.models import AgentMode, ExperimentSpec, RunRecord, fingerprint
from buildtrace.experiments.openai import OpenAIClient
from buildtrace.experiments.runner import protocol_fingerprint, run_trial, trial_id
from buildtrace.github import resolve_github_token

RESEARCH_RUNTIME_FILES = (
    "infra/research_v2/hosted-ci.yml", "infra/research_v2/hosted_verify.py",
    "infra/research_v2/prepare_hosted.py", "ci/constraints.txt",
)


def load_spec(path: Path) -> ExperimentSpec:
    spec = ExperimentSpec.model_validate_json(path.read_text(encoding="utf-8"))
    if spec.corpus is not None and not spec.corpus.is_absolute():
        spec = spec.model_copy(update={"corpus": (path.parent / spec.corpus).resolve()})
    return spec


def validate_local_inputs(spec: ExperimentSpec, store: EvidenceStore, *, split: str | None = None) -> dict:
    needs = []
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        needs.append("Configure OPENAI_API_KEY locally before live model calls.")
    if not resolve_github_token():
        needs.append("Configure GH_TOKEN/GITHUB_TOKEN or an authenticated GitHub CLI for live CI trials.")
    tasks = [task for task in spec.tasks if split is None or task.split == split]
    if not tasks:
        raise ValueError("Choose a declared train, dev, or test split")
    for task in tasks:
        try:
            capture = store.load(task.capture_id)
            store.verify(task.capture_id)
            if capture["source_ref"] != task.base_sha:
                needs.append(f"{task.id}: capture revision differs from the task.")
            if task.capture_sha256 and fingerprint(capture) != task.capture_sha256:
                needs.append(f"{task.id}: initial evidence hash differs from the task.")
        except (OSError, ValueError, RuntimeError) as exc:
            needs.append(f"{task.id}: archive its exact failing CI attempt first ({exc}).")
    memory = None
    if any(task.split in {"dev", "test"} for task in tasks):
        if spec.corpus is None:
            needs.append("Collect separate no-memory training sessions and build a frozen WikiTrace corpus.")
        else:
            try:
                memory = FrozenMemory(spec.corpus, spec.corpus_sha256)
                if memory.insights_sha256 != spec.insights_sha256:
                    raise ValueError("Injected insight hash differs")
                memory.assert_test_separation(spec)
            except (OSError, ValueError) as exc:
                needs.append(f"Resolve historical memory: {exc}")
    return {
        "schema_valid": True,
        "experiment_id": spec.id,
        "model": spec.model.model,
        "tasks_by_split": dict(Counter(task.split for task in tasks)),
        "memory_verified": memory is not None,
        "local_prerequisites_ready": not needs,
        "remaining_inputs": needs,
        "online_preflight": "Before any trial, the runner also validates and pins the trusted GitHub oracle workflow.",
    }


def schedule(spec: ExperimentSpec, split: str, include_reruns: bool = False):
    rng = random.Random(spec.seed)
    tasks = sorted((task for task in spec.tasks if task.split == split), key=lambda task: task.id)
    rng.shuffle(tasks)
    planned = []
    for task in tasks:
        modes = list(AgentMode) if split in {"dev", "test"} else [AgentMode.NO_MEMORY]
        rng.shuffle(modes)
        for trial in range(spec.trials):
            ordered = modes[trial % len(modes):] + modes[:trial % len(modes)]
            planned.extend((task, mode, trial, "reference") for mode in ordered)
            if include_reruns and split in {"dev", "test"}:
                planned.append((task, AgentMode.NO_MEMORY, trial, "rerun"))
    return planned


def run_experiment(
    spec: ExperimentSpec,
    store: EvidenceStore,
    output: Path,
    *,
    split: str,
    allow_publish: bool,
    include_reruns: bool = False,
    continue_on_infra: bool = False,
    model_factory=OpenAIClient,
    backend_factory=GitHubTrialBackend,
    memory_factory=FrozenMemory,
    phase_manifest: dict | None = None,
    evaluation_freeze: dict | None = None,
    protocol_freeze: dict | None = None,
    runtime_extra_files=None,
    registry=None,
    gate_task_ids=None,
    max_new_trials: int | None = None,
) -> list[RunRecord]:
    if not allow_publish:
        raise ValueError("Authorize experimental branches and CI runs explicitly with --allow-publish.")
    if split not in {"train", "dev", "test"} or not any(task.split == split for task in spec.tasks):
        raise ValueError("Choose a declared train, dev, or test split")
    if max_new_trials is not None and (type(max_new_trials) is not int or max_new_trials < 1):
        raise ValueError("Checkpoint size must be a positive number of new trials")
    research = spec.schema_version == 2
    if research and include_reruns:
        raise ValueError("The v2 frozen phase declares reference cells only; collect noise reruns in a separate declared cohort")
    runtime_extra_files = RESEARCH_RUNTIME_FILES if research and runtime_extra_files is None else runtime_extra_files or ()
    memory = None
    if split in {"dev", "test"}:
        if spec.corpus is None:
            raise ValueError("Three-mode evaluation requires a frozen corpus from separate training sessions")
        memory = memory_factory(spec.corpus, spec.corpus_sha256)
        if memory.insights_sha256 != spec.insights_sha256:
            raise ValueError("The experiment's insight hash differs from the frozen memory")
        memory.assert_test_separation(spec)

    with model_factory(spec.model) as model:
        pinned_tasks = []
        for task in spec.tasks:
            if task.split != split:
                pinned_tasks.append(task)
                continue
            backend = backend_factory(task, store, allow_publish=True, namespace="preflight-" + fingerprint(task)[:20])
            try:
                pinned_tasks.append(backend.pin_oracle())
            finally:
                backend.close()
        spec = spec.model_copy(update={"tasks": pinned_tasks})
        output.mkdir(parents=True, exist_ok=True)
        packets = {}
        protocol_sha256 = protocol_fingerprint()
        if research:
            from buildtrace.experiments.research import create_input_manifest, verify_freeze, verify_input_manifest
            if phase_manifest is None:
                if split == "test":
                    raise ValueError("Held-out v2 execution requires an explicit evaluation candidate and approved freeze")
                phase_manifest = create_input_manifest(
                    spec, split, memory=memory, registry=registry, extra_files=runtime_extra_files, gate_task_ids=gate_task_ids,
                )
            verify_input_manifest(phase_manifest, spec, split, memory=memory, registry=registry, extra_files=runtime_extra_files)
            phase_sha256 = phase_manifest["sha256"]
            if split == "test":
                if evaluation_freeze is None or protocol_freeze is None:
                    raise ValueError("Held-out v2 execution requires a development-approved evaluation freeze")
                phase_sha256 = verify_freeze(evaluation_freeze, phase_manifest, protocol_freeze=protocol_freeze, spec=spec)
            if spec.phase_manifest_sha256 not in {None, phase_sha256}:
                raise ValueError("The declared phase manifest differs from this execution")
            spec = spec.model_copy(update={"phase_manifest_sha256": phase_sha256})
            protocol_sha256 = phase_manifest["protocol_sha256"]

            def save_identity(name, value):
                path = output / name
                if path.exists():
                    if json.loads(path.read_text(encoding="utf-8")) != value:
                        raise ValueError(f"Frozen phase artifact changed: {name}")
                else:
                    with path.open("x", encoding="utf-8") as stream:
                        stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")

            save_identity("input-manifest.json", phase_manifest)
            if split == "test":
                save_identity("protocol-freeze.json", protocol_freeze)
                save_identity("evaluation-freeze.json", evaluation_freeze)
            if memory is not None:
                packets = {
                    task.id: memory.select_packet(task, store, spec.retrieval)
                    for task in spec.tasks if task.split == split
                }
                # Packet selection is done once before the mode/repetition grid.
                save_identity("packets.json", json.loads(json.dumps({key: value.to_dict() for key, value in packets.items()})))
        spec_file = output / "experiment.json"
        if spec_file.exists():
            if fingerprint(load_spec(spec_file)) != fingerprint(spec):
                raise ValueError("This output directory belongs to a different frozen experiment")
        else:
            with spec_file.open("x", encoding="utf-8") as stream:
                stream.write(spec.model_dump_json(indent=2) + "\n")
        planned = schedule(spec, split, include_reruns)
        records = []
        new_trials = 0

        def execute(cell):
            task, mode, trial, role = cell
            identifier = trial_id(spec, task, mode, trial, role)
            backend = backend_factory(task, store, allow_publish=True, namespace=identifier)
            try:
                if research and fingerprint(backend.pin_oracle()) != fingerprint(task):
                    raise ValueError("Trial preflight changed a frozen task/verifier identity")
                return run_trial(
                    spec, task, mode, trial, backend, model, output, memory=memory, role=role,
                    packet=packets.get(task.id) if mode != AgentMode.NO_MEMORY else None,
                    phase_manifest=phase_manifest, evaluation_freeze=evaluation_freeze, protocol_freeze=protocol_freeze,
                    runtime_extra_files=runtime_extra_files, registry=registry,
                )
            finally:
                backend.close()

        width = spec.execution_parallelism
        # Width three executes one complete rotated three-mode repeat together;
        # training uses the same width for its three independent repetitions.
        # A single coordinator writes the aggregate in declared schedule order.
        with ThreadPoolExecutor(max_workers=width) as pool:
            stop = False
            for offset in range(0, len(planned), width):
                block = []
                for cell in planned[offset:offset + width]:
                    task, mode, trial, role = cell
                    identifier = trial_id(spec, task, mode, trial, role)
                    existing = output / "trials" / identifier / "result.json"
                    if existing.exists():
                        record = RunRecord.model_validate_json(existing.read_text(encoding="utf-8"))
                        if record.experiment_sha256 != fingerprint(spec) or record.protocol_sha256 != protocol_sha256:
                            raise ValueError("Completed trials have different configuration, code, or prompts; use a new experiment ID.")
                        if research and record.phase_manifest_sha256 != spec.phase_manifest_sha256:
                            raise ValueError("A completed trial belongs to a different frozen phase")
                        block.append(record)
                    else:
                        if max_new_trials is not None and new_trials >= max_new_trials:
                            stop = True
                            break
                        block.append(pool.submit(execute, cell) if width > 1 else execute(cell))
                        new_trials += 1
                failed = []
                for value in block:
                    record = value.result() if isinstance(value, Future) else value
                    records.append(record)
                    temporary = output / "results.jsonl.pending"
                    temporary.write_text("".join(record.model_dump_json() + "\n" for record in records), encoding="utf-8")
                    temporary.replace(output / "results.jsonl")
                    if record.outcome == "infra-error":
                        failed.append(record.id)
                if failed and not continue_on_infra:
                    raise ValueError(
                        f"Trial {failed[0]} hit an infrastructure error; the entire active block was preserved. "
                        "Resolve the setup; use a new experiment ID or explicitly continue while retaining this outcome."
                    )
                if stop:
                    break
        return records


def apply_reviews(records: list[RunRecord], review_path: Path, original_bytes: bytes) -> list[RunRecord]:
    """Attach independent annotations to a derived copy, keeping original trial records intact."""
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("source_results_sha256") != hashlib.sha256(original_bytes).hexdigest():
        raise ValueError("Review annotations refer to a different results file")
    by_id = {record.id: record.model_copy(deep=True) for record in records}
    seen = set()
    for annotation in review.get("hunks", []):
        if annotation.get("classification") == "unreviewed":
            continue
        key = (annotation["run_id"], annotation["hunk_id"])
        if key in seen:
            raise ValueError("Duplicate hunk review")
        seen.add(key)
        record = by_id[annotation["run_id"]]
        hunk = next((hunk for hunk in record.hunks if hunk.id == annotation["hunk_id"]), None)
        if hunk is None or not annotation.get("reviewer") or annotation["classification"] not in {"necessary", "unnecessary", "uncertain"}:
            raise ValueError("Invalid independent hunk review")
        hunk.classification, hunk.reviewer = annotation["classification"], annotation["reviewer"]
    for annotation in review.get("evidence", []):
        if annotation.get("review") == "unreviewed":
            continue
        key = (annotation["run_id"], annotation["event_id"])
        if key in seen:
            raise ValueError("Duplicate evidence-impact review")
        seen.add(key)
        record = by_id[annotation["run_id"]]
        event = next((event for event in record.evidence_decisions if event.id == annotation["event_id"]), None)
        if event is None or not annotation.get("reviewer") or annotation["review"] not in {"confirmed-change", "confirmed-no-change", "uncertain"}:
            raise ValueError("Invalid independent evidence-impact review")
        event.review, event.reviewer = annotation["review"], annotation["reviewer"]
    return [RunRecord.model_validate(by_id[record.id].model_dump()) for record in records]


def apply_research_reviews(records: list[RunRecord], annotations: dict, mapping: dict, original_bytes: bytes) -> list[RunRecord]:
    """Attach separately blinded review labels; never accept edited source observations."""
    from buildtrace.experiments.research import content_sha256
    if mapping.get("kind") != "research-review-mapping" or mapping.get("sha256") != content_sha256({key: value for key, value in mapping.items() if key != "sha256"}):
        raise ValueError("Invalid private research review mapping")
    if mapping.get("source_results_sha256") != hashlib.sha256(original_bytes).hexdigest():
        raise ValueError("Research annotations refer to different original results")
    original_records = [RunRecord.model_validate_json(line) for line in original_bytes.decode("utf-8").splitlines() if line.strip()]
    if [record.model_dump(mode="json") for record in original_records] != [record.model_dump(mode="json") for record in records]:
        raise ValueError("Attach reviews only to the original unannotated records")
    by_id = {record.id: record.model_copy(deep=True) for record in records}
    for kind, mapping_key, allowed_fields in (
        ("hunks", "hunk_package_sha256", {"item_id", "classification", "reviewer", "rationale"}),
        ("evidence", "evidence_package_sha256", {"item_id", "material_effect", "adoption", "adoption_action_ids", "contaminated", "mechanism_reviewer", "rationale"}),
    ):
        rows = annotations.get(kind, [])
        if rows and annotations.get(mapping_key) != mapping.get(mapping_key):
            raise ValueError("Annotations refer to another blinded review package")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - allowed_fields:
                raise ValueError("Review annotations may only contain review labels and rationale")
            token = row.get("item_id")
            if token in seen or token not in mapping[kind]:
                raise ValueError("Duplicate or unknown anonymous review item")
            seen.add(token)
            link = mapping[kind][token]
            record = by_id[link["run_id"]]
            if kind == "hunks":
                target = next(hunk for hunk in record.hunks if hunk.id == link["hunk_id"])
                target.classification, target.reviewer = row["classification"], row.get("reviewer")
            else:
                target = next(event for event in record.evidence_decisions if event.id == link["event_id"])
                for name in allowed_fields - {"item_id", "rationale"}:
                    if name in row:
                        setattr(target, name, row[name])
    return [RunRecord.model_validate(by_id[record.id].model_dump(mode="json")) for record in records]


def review_template(records: list[RunRecord], original_bytes: bytes) -> dict:
    return {
        "schema_version": 1,
        "source_results_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "instructions": "Review behavior and task requirements independently. Agent mode is intentionally omitted. Leave uncertain or unreviewed changes unscored.",
        "hunks": [
            {"run_id": record.id, "task_id": record.task_id, "hunk_id": hunk.id, "path": hunk.path,
             "diff": hunk.diff, "classification": "unreviewed", "reviewer": ""}
            for record in records for hunk in record.hunks
        ],
        "evidence": [
            {"run_id": record.id, "event_id": event.id, "question": event.question,
             "before": event.before.model_dump(mode="json"),
             "after": event.after.model_dump(mode="json") if event.after else None,
             "citations": [citation.model_dump(mode="json") for citation in event.citations],
             "review": "unreviewed", "reviewer": ""}
            for record in records for event in record.evidence_decisions
        ],
    }
