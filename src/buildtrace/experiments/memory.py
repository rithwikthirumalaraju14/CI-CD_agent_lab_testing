"""CI adapter around the pinned, upstream WikiTrace compiler and corpus renderers."""

import hashlib
import importlib.metadata
import json
import math
import re
import shutil
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from wikitrace.config import CompilationInputs, ModelSpec, ModelsConfig, PromptProfile
from wikitrace.harness.llm.client import LLMClient
from wikitrace.memory.compiler import compile_trajectory, relink_compilation
from wikitrace.memory.insights import InsightItem, render_inject, render_insight_document, render_insight_index
from wikitrace.memory.store import (
    RawLayerShape, RawStepFacts, compilation_to_markdown,
    render_compilation_index, render_raw_index_from_steps,
)

from buildtrace.evidence import EvidenceError, EvidenceStore
from buildtrace.experiments.models import (
    AgentMode, EvidenceCitation, ExperimentSpec, RetrievalSettings, RunRecord, TaskSpec, fingerprint,
)
from buildtrace.experiments.openai import OpenAIClient
from buildtrace.experiments.prompts import BASE_SYSTEM
from buildtrace.models import FAILED_CONCLUSIONS

UPSTREAM_REVISION = "25a83679af0eb67ab3c835ab920d3a243fb80845"
CI_SHAPE = RawLayerShape(
    agent_logs=("solver.full.jsonl",), optional_agent_logs=(),
    outcome_files=("metadata.md", "verification.json"),
    step_contents="Each raw/step-NNNN/ contains metadata.md, solver.full.jsonl, verification.json, and original CI files under initial-ci/ and attempt-*/ directories.",
    read_hint="Read observable tool calls and brief decision notes in solver.full.jsonl; original CI files are listed in metadata.md",
    prediction_file="verification.json",
    metadata_description="One CI debugging session and its actual verification results.",
    metadata_keys=("task_id", "session_success"), task_key="task", outcome_key="session_success",
    metadata_artifacts="- solver.full.jsonl: observable debugging actions and results\n- verification.json: final outcome and CI verification records\n- initial-ci/: original task evidence",
    index_score="verified={success:.0f}", index_score_pattern=r"verified=(?P<success>\d+)",
    index_score_columns="(verified=0|1)", index_metric_names="verified repair",
    compilation_layer_desc="(session outcome, verified repair, one-line summary)",
    compilation_task_hint="repository, failing revision, CI run, and repair task",
    compilation_outcome_hint="Whether the candidate passed required CI checks; this does not establish causality.",
    compilation_correct_parts_hint="Only observations actually supported by the session.",
    compilation_description="CI debugging session; verified repair={success}.",
    compilation_metric_keys=("success",), compilation_fm_metric_lines=("success: 0 | 1",),
    writer_metric_header=(("success", "VERIFIED_REPAIR"),),
    env_compilation_sections=True, episode=True, no_memory=True,
    turn_log_text="Each solver.full.jsonl row is an observed model message, tool action, tool result, or session outcome. Decision notes are brief, explicit plans.",
)


def upstream_provenance() -> dict:
    distribution = importlib.metadata.distribution("wikitrace")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    revision = direct.get("vcs_info", {}).get("commit_id")
    if revision != UPSTREAM_REVISION:
        raise ValueError("Install BuildTrace's pinned WikiTrace dependency before compiling or using memory.")
    return {"repository": "https://github.com/yukyeongleee/wikitrace", "revision": revision, "license": "Apache-2.0"}


def _path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise ValueError("Invalid corpus path")
    destination = root.joinpath(*path.parts).resolve()
    if not destination.is_relative_to(root.resolve()) or destination == root.resolve():
        raise ValueError("Corpus path escapes its root")
    return destination


@dataclass(frozen=True)
class InsightPacket:
    task_sha256: str
    capture_sha256: str
    query_sha256: str
    corpus_sha256: str
    insights_sha256: str
    selector_sha256: str
    insight_ids: tuple[str, ...]
    text: str
    sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


_DIAGNOSTIC_STOP_WORDS = frozenset("""
a about after all also an and any are as at be because been before but by can
checks ci current did do does each for from had has have if in into is it its job
of on only or original project protected pytest run runner same should shell test
tests that the their then these this to use used using was were when where which
while with without would github candidate trusted work endgroup group
""".split())


def _tokens(text: str, profile: str = "casefold-identifiers-v1") -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).casefold()
    tokens = []
    for word in re.findall(r"[a-z_][a-z0-9_]*", text):
        if len(word) > 1:
            tokens.append(word)
        tokens.extend(part for part in word.split("_") if part != word and len(part) > 1)
    return [token for token in tokens if token not in _DIAGNOSTIC_STOP_WORDS] if profile == "diagnostic-identifiers-v2" else tokens


def _initial_query(capture: dict, store: EvidenceStore, rule: str) -> str:
    """Derive retrieval solely from the pinned initial CI attempt.

    The original rule is retained for replay. V2 ignores successful bootstrap
    logs, duplicate aggregate logs, command/environment wrappers, and downstream
    finalization errors when a primary failed-step log is available.
    """
    names, primary = [], []
    for job in capture["jobs"]:
        if job.get("conclusion") not in FAILED_CONCLUSIONS:
            continue
        failed = [step for step in job.get("steps", []) if step.get("conclusion") in FAILED_CONCLUSIONS]
        names.append(job.get("name") or "")
        if rule == "initial-failed-logs-and-steps-v1":
            names.extend(step.get("name") or "" for step in failed)
        elif failed:
            first = min(enumerate(failed), key=lambda pair: pair[1].get("number", pair[0]))[1]
            names.append(first.get("name") or "")
            primary.append((job.get("name") or "", first.get("name") or ""))
    logs = sorted((entry for entry in capture["entries"] if entry["kind"] == "log"), key=lambda row: row["id"])
    selected = []
    if rule != "initial-failed-logs-and-steps-v1":
        for entry in logs:
            label = entry["label"].replace("\\", "/")
            filename = re.sub(r"^\d+_", "", PurePosixPath(label).name).removesuffix(".txt")
            if any(filename == step and label.startswith(job + "/") for job, step in primary):
                selected.append(entry)
    fallback = not selected
    selected = selected or logs
    seen = set()
    for entry in selected:
        if entry["sha256"] in seen:
            continue
        seen.add(entry["sha256"])
        _, content = store.read_entry(capture["id"], entry["id"])
        text = content.decode("utf-8", errors="replace")
        if rule == "initial-failed-logs-and-steps-v1":
            names.append(text)
            continue
        lines = []
        for line in text.splitlines():
            line = re.sub(r"^\ufeff?\d{4}-\d{2}-\d{2}T\S+\s*", "", line)
            lines.append(re.sub(r"\x1b\[[0-9;]*m", "", line))
        if not fallback:
            endgroup = next((index for index, line in enumerate(lines) if line.strip() == "##[endgroup]"), None)
            if endgroup is not None:
                lines = lines[endgroup + 1:]
        else:
            # Archives without individual step files still need a deterministic,
            # observation-only fallback rather than all successful setup output.
            anchors = [index for index, line in enumerate(lines) if re.search(r"(?:Traceback|\w*(?:Error|Exception):|\bFAILED\b|\bERROR:|\bFAILURES\b)", line)]
            keep = {other for index in anchors for other in range(max(0, index - 2), min(len(lines), index + 4))}
            lines = [line for index, line in enumerate(lines) if index in keep]
        names.extend(line for line in lines if not line.lstrip().startswith(("##[", "shell:", "env:", "Run "))
                     and not re.match(r"^\[[a-z_]+\]\s+.*(?:python|\bpip\b)", line))
    return "\n".join(names)


class FrozenMemory:
    def __init__(self, directory: Path | str, expected_sha256: str | None = None):
        upstream_provenance()
        self.directory = Path(directory).resolve()
        self.manifest = json.loads((self.directory / "corpus.json").read_text(encoding="utf-8"))
        if self.manifest.get("schema_version") != 1 or self.manifest.get("upstream", {}).get("revision") != UPSTREAM_REVISION:
            raise ValueError("Unsupported memory corpus or upstream revision")
        self.sha256 = fingerprint(self.manifest)
        if expected_sha256 is not None and self.sha256 != expected_sha256:
            raise ValueError("Frozen corpus hash differs from the experiment")
        self.items = [
            InsightItem(**(item | {"sections": tuple(tuple(section) for section in item["sections"]), "apps": tuple(item["apps"])}))
            for item in self.manifest["insights"]
        ]
        if not self.items or len({item.id for item in self.items}) != len(self.items):
            raise ValueError("The corpus must contain uniquely identified insights")
        self.insights_text = render_inject(self.items, source=True)
        self.insights_sha256 = hashlib.sha256(self.insights_text.encode("utf-8")).hexdigest()
        if self.insights_sha256 != self.manifest["insights_sha256"]:
            raise ValueError("The injected insight text differs from the frozen corpus")
        self.verify()

    def verify(self):
        for relative, expected in self.manifest["files"].items():
            if hashlib.sha256(_path(self.directory, relative).read_bytes()).hexdigest() != expected:
                raise ValueError(f"Frozen memory file was modified: {relative}")

    def assert_test_separation(self, spec: ExperimentSpec):
        if self.manifest.get("data_kind") == "synthetic" and spec.data_kind == "live":
            raise ValueError("Synthetic histories cannot be used as live experimental memory")
        if self.manifest.get("compiler_model") != spec.model.model_dump(mode="json"):
            raise ValueError("The corpus compiler model/settings differ from this experiment")
        tests = [task for task in spec.tasks if task.split in {"dev", "test"}]
        for source in self.manifest["training_tasks"]:
            if any(task.id == source["id"] or (task.repository, task.base_sha) == (source["repository"], source["base_sha"]) for task in tests):
                raise ValueError("Test task leaked into the historical memory corpus")
            if spec.hold_out_families and any(task.family == source["family"] for task in tests):
                raise ValueError("A held-out scenario family is present in memory")
        if spec.schema_version == 2:
            from buildtrace.experiments.research import create_split_registry
            create_split_registry(
                [*spec.tasks, *({**source, "split": "train"} for source in self.manifest["training_tasks"])],
                spec.split_registry,
            )

    def select_packet(self, task: TaskSpec, store: EvidenceStore, settings: RetrievalSettings) -> InsightPacket:
        """Rank only frozen initial CI observations against insight prose.

        Neither operator family labels nor later solver actions/outcomes enter
        the query. The two memory modes call this same deterministic operation.
        """
        capture = store.load(task.capture_id)
        store.verify(task.capture_id)
        capture_sha256 = fingerprint(capture)
        if capture["source_ref"] != task.base_sha or (task.capture_sha256 and task.capture_sha256 != capture_sha256):
            raise ValueError("Packet selection requires the task's frozen initial capture")
        query = sorted(set(_tokens(_initial_query(capture, store, settings.query), settings.tokenizer)))
        documents = [Counter(_tokens(item.title + "\n" + "\n".join(text for _, text in item.sections), settings.tokenizer)) for item in self.items]
        lengths = [sum(document.values()) for document in documents]
        mean_length = sum(lengths) / len(lengths)
        frequencies = Counter(term for document in documents for term in document)
        scores = []
        for item, document, length in zip(self.items, documents, lengths):
            score = 0.0
            for term in query:
                count = document.get(term, 0)
                if count:
                    inverse = math.log(1 + (len(documents) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                    score += inverse * count * (settings.k1 + 1) / (count + settings.k1 * (1 - settings.b + settings.b * length / mean_length))
            if score > 0:
                scores.append((score, item))
        sources = {
            source.get("source_step", f"step-{index:04d}"): (source["repository"], source["id"])
            for index, source in enumerate(self.manifest["training_tasks"])
        }
        selected, used_tasks = [], set()
        for _, item in sorted(scores, key=lambda pair: (-pair[0], pair[1].id)):
            source_task = sources[item.source_step]
            if source_task in used_tasks:
                continue
            candidate = render_inject([*selected, item], source=True)
            if len(candidate) > settings.max_characters:
                continue
            selected.append(item)
            used_tasks.add(source_task)
            if len(selected) == settings.max_insights:
                break
        text = render_inject(selected, source=True) if selected else ""
        # Upstream reorders items. Hash and record actual delivery, not relevance order.
        ordered_ids = tuple(re.findall(r'<insight id="([^"]+)">', text))
        if set(ordered_ids) != {item.id for item in selected} or len(ordered_ids) != len(selected):
            raise ValueError("The upstream insight renderer changed its packet format")
        return InsightPacket(
            task_sha256=fingerprint(task), capture_sha256=capture_sha256,
            query_sha256=fingerprint({"terms": query, "query_rule": settings.query, "tokenizer": settings.tokenizer}),
            corpus_sha256=self.sha256, insights_sha256=self.insights_sha256,
            selector_sha256=fingerprint(settings), insight_ids=ordered_ids, text=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def read(self, insight_id: str, layer: str, document: str = "", offset: int = 0, limit: int = 12000, *, allowed_insight_ids=None, needle: str = ""):
        if allowed_insight_ids is not None and insight_id not in allowed_insight_ids:
            raise ValueError("Historical reads are restricted to the assigned insight packet")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
            raise ValueError("Invalid historical evidence range")
        if not isinstance(needle, str) or len(needle) > 120 or (needle and not needle.strip()):
            raise ValueError("Use a short, nonblank literal historical search term")
        if needle:
            if layer != "raw" or document not in {"", "solver.full.jsonl"} or limit < len(needle):
                raise ValueError("Search only linked raw solver observations with enough excerpt space")
            document = "solver.full.jsonl"
        item = next((item for item in self.items if item.id == insight_id), None)
        if item is None:
            raise ValueError("Unknown insight ID")
        if layer == "compilation":
            if document:
                raise ValueError("A compilation has a single source document")
            relative = f"compilation/{item.source_step}.md"
        elif layer == "raw":
            relative = f"raw/{item.source_step}/{document or 'metadata.md'}"
        else:
            raise ValueError("Read compilation or raw historical evidence")
        if relative not in self.manifest["files"] or relative.endswith(".zip"):
            raise ValueError("The requested historical text document is not in the linked source run")
        content = _path(self.directory, relative).read_bytes()
        expected = self.manifest["files"][relative]
        if hashlib.sha256(content).hexdigest() != expected:
            raise EvidenceError("Historical evidence checksum mismatch")
        text = content.decode("utf-8", errors="replace")
        if offset >= len(text) and offset != 0:
            raise ValueError("Historical evidence offset is outside the document")
        delivery_limit = limit
        if needle:
            # Search only prior successful file/log observations. A match in an
            # assistant's guess, plan, patch, or model-written compilation is
            # not evidence of what the previous solver actually observed.
            found = False
            position = 0
            pattern = re.compile(re.escape(needle), re.IGNORECASE)
            for line in text.splitlines(keepends=True):
                if position + len(line) <= offset:
                    position += len(line)
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    position += len(line)
                    continue
                if not isinstance(row, dict):
                    position += len(line)
                    continue
                name = row.get("name")
                if row.get("type") != "function_call" or row.get("is_error") is not False or name not in {"read_current_file", "read_current_log"}:
                    position += len(line)
                    continue
                result = row.get("result")
                payload = result.get("content" if name == "read_current_file" else "text") if isinstance(result, dict) else None
                if not isinstance(payload, str) or not payload.strip():
                    position += len(line)
                    continue
                encoded = json.dumps(payload, ensure_ascii=False)
                encoded_at = line.find(encoded)
                if encoded_at < 0:
                    position += len(line)
                    continue
                payload_start = position + encoded_at + 1
                payload_end = position + encoded_at + len(encoded) - 1
                for match in pattern.finditer(payload):
                    encoded_prefix = json.dumps(payload[:match.start()], ensure_ascii=False)[1:-1]
                    match_at = payload_start + len(encoded_prefix)
                    if match_at < offset:
                        continue
                    offset = max(payload_start, match_at - min(1200, limit // 4))
                    delivery_limit = min(limit, payload_end - offset)
                    found = True
                    break
                if found:
                    break
                position += len(line)
            if not found:
                raise ValueError("No linked original file/log observation contains the search term")
        excerpt = text[offset:offset + delivery_limit]
        first_line = text[:offset].count("\n") + 1
        citation = EvidenceCitation(
            insight_id=insight_id, source_run_id=item.source_step, layer=layer,
            document=f"/work/corpus/{relative}", first_line=first_line,
            last_line=first_line + excerpt.count("\n"), document_sha256=expected,
        )
        return {
            "text": excerpt, "offset": offset,
            "delivered_characters": len(excerpt), "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            "next_offset": offset + len(excerpt) if offset + len(excerpt) < len(text) else None,
            "citation": citation.model_dump(mode="json"),
        }, citation


def build_memory(
    spec: ExperimentSpec,
    records: list[RunRecord],
    records_root: Path,
    store: EvidenceStore,
    output: Path,
    transport: OpenAIClient,
) -> FrozenMemory:
    """Use actual upstream compilation and rendering on complete no-memory training sessions."""
    upstream = upstream_provenance()
    tasks = {task.id: task for task in spec.tasks if task.split == "train"}
    if not records or output.exists():
        raise ValueError("Supply training records and a new corpus output directory")
    model = ModelSpec(
        provider="openai", model=spec.model.model, temperature=spec.model.temperature if spec.model.temperature is not None else 0,
        max_tokens=spec.budget.max_output_tokens, reasoning_effort=spec.model.reasoning_effort or "",
    )
    llm = LLMClient(ModelsConfig(default=model), transport=transport, max_retries=0)
    profile = PromptProfile(
        task_kind="Python CI build and test failure debugging", solver_prompt=BASE_SYSTEM, has_evidence=False,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".pending-{uuid.uuid4().hex}"
    staging.mkdir()
    files, items, compilations, training_tasks, compiler_calls, admissions = {}, [], [], [], [], []

    def write(relative, content):
        if isinstance(content, str):
            content = content.encode("utf-8")
        path = _path(staging, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files[relative] = hashlib.sha256(content).hexdigest()

    try:
        ordered = sorted(records, key=lambda record: (record.task_id, record.trial, record.id))
        if len({record.id for record in ordered}) != len(ordered):
            raise ValueError("Duplicate source debugging sessions")
        for record in ordered:
            task = tasks.get(record.task_id)
            if (
                task is None or record.task_sha256 != fingerprint(task)
                or record.experiment_sha256 != fingerprint(spec)
                or record.model != spec.model or record.budget != spec.budget
                or record.mode != AgentMode.NO_MEMORY or record.role != "reference"
                or any(hunk.classification != "unreviewed" for hunk in record.hunks)
            ):
                raise ValueError("Memory sources must be original, unannotated no-memory runs on declared training tasks")
            admitted = record.outcome in {"solved", "unsolved"}
            admissions.append({
                "source_run_id": record.id, "task_id": task.id, "outcome": record.outcome,
                "record_sha256": fingerprint(record), "admitted": admitted,
                "reason": "complete-agent-session" if admitted else "infrastructure-or-unreproduced-input",
            })
            if not admitted:
                if spec.schema_version == 1:
                    raise ValueError("Memory sources must be complete no-memory agent sessions")
                continue
            number = len(training_tasks)
            trajectory = _path(records_root.resolve(), record.trajectory_path).read_bytes()
            step = f"step-{number:04d}"
            root = f"raw/{step}"
            write(f"{root}/solver.full.jsonl", trajectory)
            write(f"{root}/verification.json", record.model_dump_json(indent=2))
            capture = store.load(task.capture_id)
            if capture["source_ref"] != task.base_sha:
                raise ValueError("The training run's initial CI snapshot differs from its task")
            evidence_paths = []
            captures = [("initial-ci", task.capture_id)]
            for verification in record.verifications:
                if verification.capture_id:
                    captures.append((f"attempt-{verification.attempt}-target-ci", verification.capture_id))
                if verification.oracle_capture_id:
                    captures.append((f"attempt-{verification.attempt}-oracle-ci", verification.oracle_capture_id))
            for folder, capture_id in captures:
                original = store.load(capture_id)
                write(f"{root}/{folder}/manifest.json", json.dumps(original, indent=2))
                for entry in original["entries"]:
                    _, content = store.read_entry(capture_id, entry["id"])
                    relative = f"{root}/{folder}/{entry['path']}"
                    write(relative, content)
                    evidence_paths.append(relative)
            metadata = (
                f"# {task.id}\n- task: Repair the failed Python build while preserving required behavior.\n"
                f"- repository: {task.repository}\n- base_sha: {task.base_sha}\n"
                f"- initial_run: {task.initial_run_url}\n- session_success: {int(record.success)}\n"
                f"- outcome: {record.outcome}\n- source_run_id: {record.id}\n\n"
                "## Session records\n- solver.full.jsonl: observable investigation\n"
                "- verification.json: session outcome and CI verification records\n\n"
                "## Original CI evidence (initial and verification runs)\n"
                + "\n".join(f"- /work/corpus/{path}" for path in evidence_paths) + "\n"
            )
            write(f"{root}/metadata.md", metadata)
            inputs = CompilationInputs(
                sample_id=record.id, db_id=task.repository, success=record.success,
                metrics={"success": float(record.success)},
                question=f"Repair the failed Python CI run {task.initial_run_url}",
                prediction=record.verifications[-1].candidate_sha if record.verifications else "",
                task_fields=(("repository", task.repository), ("base_sha", task.base_sha), ("initial_run", task.initial_run_url)),
            )
            result = compile_trajectory(
                llm, inputs, number, trajectory_text=trajectory.decode("utf-8"),
                outcome_text=json.dumps({"outcome": record.outcome, "stop_reason": record.stop_reason,
                                         "verifications": [value.model_dump(mode="json") for value in record.verifications]}),
                profile=profile,
            )
            candidates = result.structured.parsed.candidate_insights
            if not 1 <= len(candidates) <= 3:
                raise ValueError("The compiler must return between one and three source-linked insights")
            step_items = [
                InsightItem(
                    id=f"insight-{len(items) + index:04d}", title=candidate.title,
                    sections=(("Insight", candidate.guideline), ("Applicability", candidate.applicability)),
                    source_step=step, outcome="success" if record.success else "failure",
                    task=inputs.question, task_type=task.category,
                )
                for index, candidate in enumerate(candidates)
            ]
            items.extend(step_items)
            compilation = result.compilation
            compilation.body = relink_compilation(compilation.body, step_items)
            compilations.append(compilation)
            write(f"compilation/{step}.md", compilation_to_markdown(compilation, raw_shape=CI_SHAPE))
            for item in step_items:
                write(f"insight/{item.id}.md", render_insight_document(item, task_type=True))
            source_task = {"id": task.id, "family": task.family, "repository": task.repository, "base_sha": task.base_sha, "source_run_id": record.id}
            if spec.schema_version == 2:
                source_task.update(source_step=step, record_sha256=fingerprint(record), task_sha256=record.task_sha256,
                                   phase_manifest_sha256=record.phase_manifest_sha256, outcome=record.outcome)
            training_tasks.append(source_task)
            compiler_calls.append(result.structured.record.model_dump(mode="json"))
        if not items:
            raise ValueError("No complete agent sessions were admitted to the corpus")
        write("insight/_index.md", render_insight_index(items))
        write("compilation/_index.md", render_compilation_index(compilations, entry_source="title", raw_shape=CI_SHAPE))
        write("raw/_index.md", render_raw_index_from_steps([
            (comp.step_id, RawStepFacts(outcome=comp.outcome.value, tail=comp.question, metrics=comp.metrics))
            for comp in compilations
        ], raw_shape=CI_SHAPE))
        manifest = {
            "schema_version": 1, "data_kind": spec.data_kind, "upstream": upstream, "compiler_model": spec.model.model_dump(mode="json"),
            "training_tasks": training_tasks, "insights": [asdict(item) for item in items],
            "insights_sha256": hashlib.sha256(render_inject(items, source=True).encode("utf-8")).hexdigest(),
            "compiler_calls": compiler_calls, "files": files,
        }
        if spec.schema_version == 2:
            manifest["training_admission_ledger"] = admissions
        (staging / "corpus.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        staging.rename(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return FrozenMemory(output)
