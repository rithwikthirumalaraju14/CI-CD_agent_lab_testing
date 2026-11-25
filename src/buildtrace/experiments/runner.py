"""The same solver loop in all three modes; only historical access differs."""

import hashlib
import json
import time
from pathlib import Path

from buildtrace.evidence import EvidenceError
from buildtrace.experiments.backend import BackendError, safe_repository_path
from buildtrace.experiments.memory import FrozenMemory, InsightPacket
from buildtrace.experiments.models import (
    AgentMode, Decision, EvidenceDecision, ExperimentSpec, HistoricalRead, ObservableAction,
    RunRecord, TaskSpec, Timing, fingerprint,
)
from buildtrace.experiments.openai import OpenAIError
from buildtrace.experiments.prompts import BASE_SYSTEM, CI_DESCENT, RESEARCH_GUIDANCE, tool_definitions


def protocol_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_text(encoding="utf-8").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def trial_id(spec: ExperimentSpec, task: TaskSpec, mode: AgentMode, trial: int, role="reference") -> str:
    return "run-" + fingerprint([fingerprint(spec), task.id, mode.value, trial, role])[:24]


def _reject_json_constant(value):
    raise ValueError(f"Use finite JSON values; {value} is not a valid tool argument")


def run_trial(
    spec: ExperimentSpec,
    task: TaskSpec,
    mode: AgentMode,
    trial: int,
    backend,
    model,
    output: Path,
    *,
    memory: FrozenMemory | None = None,
    role: str = "reference",
    packet: InsightPacket | None = None,
    phase_manifest: dict | None = None,
    evaluation_freeze: dict | None = None,
    protocol_freeze: dict | None = None,
    runtime_extra_files=(),
    registry=None,
) -> RunRecord:
    research = spec.schema_version == 2
    if not any(current.id == task.id and fingerprint(current) == fingerprint(task) for current in spec.tasks):
        raise ValueError("The requested task is not a member of the experiment")
    if not 0 <= trial < spec.trials:
        raise ValueError("Trial index is outside the experiment")
    if mode != AgentMode.NO_MEMORY:
        if memory is None or (memory.sha256, memory.insights_sha256) != (spec.corpus_sha256, spec.insights_sha256):
            raise ValueError("Memory modes must receive the same frozen corpus and identical insights")
        if task.split in {"dev", "test"}:
            memory.assert_test_separation(spec)
    if task.split == "train" and mode != AgentMode.NO_MEMORY:
        raise ValueError("Collect training sessions without historical memory")
    if model.settings != spec.model:
        raise ValueError("The solver model settings differ from the experiment")
    protocol_sha256 = protocol_fingerprint()
    if research:
        from buildtrace.experiments.research import verify_freeze, verify_input_manifest
        if role != "reference" or phase_manifest is None:
            raise ValueError("V2 trials require their predeclared reference phase manifest")
        verify_input_manifest(phase_manifest, spec, task.split, memory=memory, registry=registry, extra_files=runtime_extra_files)
        phase_sha256 = phase_manifest["sha256"]
        if task.split == "test":
            if evaluation_freeze is None or protocol_freeze is None:
                raise ValueError("Held-out v2 execution requires a development-approved evaluation freeze")
            phase_sha256 = verify_freeze(evaluation_freeze, phase_manifest, protocol_freeze=protocol_freeze, spec=spec)
        if spec.phase_manifest_sha256 != phase_sha256:
            raise ValueError("The spec points to a different frozen phase")
        protocol_sha256 = phase_manifest["protocol_sha256"]
        if mode != AgentMode.NO_MEMORY:
            if packet is None or (
                packet.task_sha256 != fingerprint(task) or packet.capture_sha256 != task.capture_sha256
                or packet.corpus_sha256 != spec.corpus_sha256 or packet.insights_sha256 != spec.insights_sha256
                or packet.selector_sha256 != fingerprint(spec.retrieval)
                or packet.sha256 != hashlib.sha256(packet.text.encode("utf-8")).hexdigest()
            ):
                raise ValueError("Memory modes need the task's frozen, byte-identical selected insight packet")
        elif packet is not None:
            raise ValueError("No-memory trials cannot receive a historical packet")

    identifier = trial_id(spec, task, mode, trial, role)
    relative_trajectory = f"trials/{identifier}/trajectory.jsonl"
    directory = output / "trials" / identifier
    directory.mkdir(parents=True, exist_ok=False)
    journal = (directory / "trajectory.jsonl").open("x", encoding="utf-8", newline="\n")
    start = time.monotonic()
    deadline = start + spec.budget.wall_seconds
    ci_wait = 0.0
    verifications, hunks, evidence_decisions, actions, evidence_reads = [], [], [], [], []
    model_calls = input_tokens = output_tokens = 0
    reasoning_tokens = cached_input_tokens = 0
    returned_models = set()
    current_plan = None
    pending_evidence = None
    history_read_ids = []
    current_plan_action = None
    last_current_sequence = -1
    patch_attempts = 0
    active_action = active_call = None
    outcome, stop_reason = "unsolved", "agent-stopped"

    def log(row):
        journal.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        journal.flush()

    def finish_evidence(after=None, after_action_id=None, *, contaminated=False):
        nonlocal pending_evidence
        if pending_evidence is not None:
            pending_evidence.after = after
            if research:
                pending_evidence.after_action_id = after_action_id
                pending_evidence.contaminated = contaminated
            evidence_decisions.append(pending_evidence)
            pending_evidence = None

    def action(kind, *, decision=None, paths=()):
        if not research:
            return None
        valid_paths = []
        for path in paths:
            try:
                valid_paths.append(safe_repository_path(path))
            except ValueError:
                pass
        value = ObservableAction(
            id=f"action-{len(actions):04d}", sequence=len(actions), kind=kind,
            decision=decision.model_copy(deep=True) if decision is not None else None, paths=valid_paths,
        )
        actions.append(value)
        return value

    tools = tool_definitions(mode == AgentMode.WIKITRACE)
    allowed = {tool["function"]["name"] for tool in tools}
    system = BASE_SYSTEM + ("\n" + RESEARCH_GUIDANCE if research else "\n" + CI_DESCENT if mode == AgentMode.WIKITRACE else "")

    try:
        observation = backend.initial_observation()
        user = {
            "task": "Repair this CI failure while preserving required behavior and the original tests.",
            "current": observation, "budget": spec.budget.model_dump(mode="json"),
            "editable_paths": task.editable_paths, "protected_paths": task.protected_paths,
        }
        initial_text = json.dumps(user, ensure_ascii=False)
        if mode != AgentMode.NO_MEMORY:
            initial_text += "\n<memory_documents>\n" + (packet.text if research else memory.insights_text) + "\n</memory_documents>"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": initial_text}]
        for message in messages:
            log({"type": "message", **message})
        if research:
            log({"type": "phase_inputs", "phase_manifest_sha256": spec.phase_manifest_sha256,
                 "protocol_sha256": protocol_sha256, "packet": packet.to_dict() if packet else None})
        while model_calls < spec.budget.model_calls:
            active_action = active_call = None
            remaining = deadline - time.monotonic()
            tokens_left = spec.budget.total_tokens - input_tokens - output_tokens
            if remaining <= 0 or tokens_left < 128:
                stop_reason = "time-budget" if remaining <= 0 else "token-budget"
                break
            model_calls += 1
            turn = model.complete(
                messages, tools=tools,
                max_tokens=min(spec.budget.max_output_tokens, tokens_left),
                timeout=min(spec.request_timeout_seconds, remaining),
            )
            input_tokens += turn.input_tokens
            output_tokens += turn.output_tokens
            reasoning_tokens += turn.reasoning_tokens
            cached_input_tokens += turn.cached_input_tokens
            returned_models.add(turn.returned_model)
            if len(returned_models) > 1:
                raise OpenAIError("The served model version changed during this trial")
            raw_calls = turn.message.get("tool_calls") or []
            assistant = {"role": "assistant", "content": turn.message.get("content")}
            if raw_calls:
                assistant["tool_calls"] = raw_calls
            if turn.continuation_items:
                messages.extend(turn.continuation_items)
            else:
                messages.append(assistant)
            log({"type": "message", **assistant, "usage": {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens, "reasoning_tokens": turn.reasoning_tokens, "cached_input_tokens": turn.cached_input_tokens}, "returned_model": turn.returned_model})
            if turn.stop_reason:
                stop_reason = "model-output-" + turn.stop_reason
                break
            if not raw_calls:
                stop_reason = "agent-stopped"
                break
            if len(raw_calls) != 1:
                raise OpenAIError("The model returned parallel actions despite the shared serial-action protocol")
            call = raw_calls[0]
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not isinstance(call.get("function"), dict):
                raise OpenAIError("The model returned an invalid tool-call envelope")
            name = call.get("function", {}).get("name", "")
            is_error = False
            arguments = {}
            read_record = None
            try:
                parsed = json.loads(call["function"]["arguments"], parse_constant=_reject_json_constant)
                json.dumps(parsed, allow_nan=False)
                arguments = parsed
                if not isinstance(arguments, dict) or name not in allowed:
                    raise ValueError("This tool is not available in the assigned agent mode")
                if time.monotonic() >= deadline:
                    stop_reason = "time-budget"
                    break
                active_call = {"name": name, "arguments": arguments}
                if name == "set_plan":
                    current_plan = Decision.model_validate(arguments)
                    current_plan_action = action("plan", decision=current_plan)
                    finish_evidence(current_plan, current_plan_action.id if current_plan_action else None)
                    history_read_ids = []
                    result = {"recorded": current_plan.model_dump(mode="json")}
                    if research:
                        result["action_id"] = current_plan_action.id
                elif name == "read_historical_evidence":
                    if mode != AgentMode.WIKITRACE or memory is None:
                        raise ValueError("Historical source evidence is not accessible in this mode")
                    active_action = action("history-read", decision=current_plan)
                    if research:
                        read_record = HistoricalRead(
                            id=active_action.id, sequence=active_action.sequence,
                            insight_id=str(arguments.get("insight_id", "")), layer=str(arguments.get("layer", "")),
                            document=str(arguments.get("document", "")), question=str(arguments.get("question", "")),
                            trigger=str(arguments.get("trigger", "")), status="error", error="Request not completed",
                            offset=arguments.get("offset", 0) if type(arguments.get("offset", 0)) is int and arguments.get("offset", 0) >= 0 else None,
                            requested_limit=arguments.get("limit", 12000) if type(arguments.get("limit", 12000)) is int and arguments.get("limit", 12000) >= 0 else None,
                        )
                        evidence_reads.append(read_record)
                        history_read_ids.append(read_record.id)
                    if current_plan is None:
                        raise ValueError("Use set_plan before inspecting historical evidence")
                    if research and (current_plan_action is None or current_plan_action.sequence <= last_current_sequence):
                        raise ValueError("Record a fresh set_plan after current observations before historical inspection")
                    if research and arguments.get("insight_id") not in packet.insight_ids:
                        raise ValueError("Historical reads are restricted to the assigned insight packet")
                    result, citation = memory.read(
                        arguments["insight_id"], arguments["layer"], arguments.get("document", ""),
                        arguments.get("offset", 0), arguments.get("limit", 12000),
                        needle=arguments.get("needle", ""),
                    )
                    if research:
                        read_record.status, read_record.error = "delivered", None
                        read_record.document, read_record.source_run_id = citation.document, citation.source_run_id
                        read_record.offset = result["offset"]
                        read_record.delivered_characters = len(result["text"])
                        read_record.document_sha256 = citation.document_sha256
                        read_record.excerpt_sha256 = hashlib.sha256(result["text"].encode("utf-8")).hexdigest()
                        read_record.first_line, read_record.last_line = citation.first_line, citation.last_line
                        result["read_id"] = read_record.id
                    if pending_evidence is None:
                        pending_evidence = EvidenceDecision(
                            schema_version=2 if research else 1,
                            id=f"read-{len(evidence_decisions):04d}",
                            question=arguments["question"], trigger=arguments["trigger"],
                            before=current_plan.model_copy(deep=True), citations=[citation],
                            read_ids=list(history_read_ids) if research else [],
                            before_action_id=current_plan_action.id if research else None,
                        )
                    else:
                        pending_evidence.citations.append(citation)
                        if research:
                            pending_evidence.read_ids = list(history_read_ids)
                elif name == "submit_patch":
                    if current_plan is None or pending_evidence is not None:
                        raise ValueError("Record the current actionable plan with set_plan before patching")
                    if (patch_attempts if research else len(verifications)) >= spec.budget.patch_attempts:
                        stop_reason = "patch-budget"
                        break
                    patch_attempts += 1
                    active_action = action("patch", decision=current_plan, paths=[change.get("path") for change in arguments["changes"] if isinstance(change, dict)])
                    patch = backend.submit_patch(arguments["changes"], timeout_seconds=max(0.01, deadline - time.monotonic()))
                    verifications.append(patch.verification)
                    ci_wait += patch.verification.elapsed_seconds
                    hunks = patch.hunks
                    result = patch.observation
                    if research:
                        active_action.verification_attempt = patch.verification.attempt
                        last_current_sequence = active_action.sequence
                elif name == "list_current_files":
                    finish_evidence(contaminated=True)
                    history_read_ids = []
                    active_action = action("current-observation", decision=current_plan)
                    result = {"files": backend.list_files()}
                elif name == "read_current_file":
                    finish_evidence(contaminated=True)
                    history_read_ids = []
                    active_action = action("current-observation", decision=current_plan, paths=[arguments["path"]])
                    result = {"path": arguments["path"], "content": backend.read_file(arguments["path"])}
                elif name == "read_current_log":
                    finish_evidence(contaminated=True)
                    history_read_ids = []
                    active_action = action("current-observation", decision=current_plan, paths=["logs/" + str(arguments["entry_id"])])
                    result = backend.read_log(arguments["entry_id"], arguments.get("offset", 0), arguments.get("limit", 12000))
                else:
                    raise ValueError("Unknown solver tool")
            except (ValueError, KeyError, TypeError) as exc:
                is_error = True
                result = {"error": str(exc)}
                if read_record is not None:
                    read_record.status = "error"
                    read_record.error = str(exc)
                    if pending_evidence is not None:
                        pending_evidence.read_ids = list(history_read_ids)
                if active_action is not None:
                    active_action.succeeded = False
            if research and active_action is not None:
                if active_action.kind == "current-observation":
                    last_current_sequence = active_action.sequence
                log({"type": "observable_action", **active_action.model_dump(mode="json")})
            log({"type": "function_call", "name": name, "arguments": arguments, "result": result, "is_error": is_error})
            if turn.continuation_items:
                messages.append({"type": "function_call_output", "call_id": call["id"], "output": json.dumps(result, ensure_ascii=False)})
            else:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            if verifications and verifications[-1].passed:
                outcome, stop_reason = "solved", "verified-by-target-ci-and-trusted-oracle"
                break
        else:
            stop_reason = "model-call-budget"
    except BackendError as exc:
        ci_wait += exc.elapsed_seconds
        if exc.verification is not None:
            verifications.append(exc.verification)
            if active_action is not None and active_action.kind == "patch":
                active_action.verification_attempt = exc.verification.attempt
        if exc.hunks:
            hunks = exc.hunks
        if active_action is not None:
            active_action.succeeded = False
        outcome, stop_reason = "infra-error", str(exc)
        if active_call:
            log({"type": "function_call", **active_call, "result": exc.observation or {"infrastructure_error": exc.as_dict()}, "is_error": True})
        log({"type": "infrastructure_error", **exc.as_dict(), "observation": exc.observation})
    except (OpenAIError, EvidenceError, OSError) as exc:
        outcome, stop_reason = "infra-error", str(exc)
        log({"type": "infrastructure_error", "message": str(exc)})
    finally:
        finish_evidence()
        wall = time.monotonic() - start
        record = RunRecord(
            schema_version=spec.schema_version,
            id=identifier, experiment_id=spec.id, experiment_sha256=fingerprint(spec), protocol_sha256=protocol_sha256,
            task_id=task.id, task_sha256=fingerprint(task), mode=mode, trial=trial, role=role,
            model=spec.model, budget=spec.budget,
            corpus_sha256=memory.sha256 if mode != AgentMode.NO_MEMORY else None,
            insights_sha256=memory.insights_sha256 if mode != AgentMode.NO_MEMORY else None,
            outcome=outcome, stop_reason=stop_reason,
            timing=Timing(wall_seconds=wall, ci_wait_seconds=ci_wait),
            model_calls=model_calls, input_tokens=input_tokens, output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens, cached_input_tokens=cached_input_tokens,
            returned_models=sorted(returned_models), verifications=verifications,
            hunks=hunks, evidence_decisions=evidence_decisions, trajectory_path=relative_trajectory,
            split=task.split if research else None, phase_manifest_sha256=spec.phase_manifest_sha256 if research else None,
            packet_sha256=packet.sha256 if research and packet else None,
            packet_insight_ids=list(packet.insight_ids) if research and packet else [],
            packet_query_sha256=packet.query_sha256 if research and packet else None,
            actions=actions, evidence_reads=evidence_reads, patch_attempts=patch_attempts,
        )
        log({"type": "session_outcome", "record": record.model_dump(mode="json")})
        journal.close()
    with (directory / "result.json").open("x", encoding="utf-8") as stream:
        stream.write(record.model_dump_json(indent=2) + "\n")
    return record
