"""Shared, validated records for a paired three-agent repair experiment."""

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_serializer, model_validator

from buildtrace.models import RunReference

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Count = Annotated[StrictInt, Field(ge=0)]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


def fingerprint(value: BaseModel | dict | list) -> str:
    if isinstance(value, ExperimentSpec):
        # Memory content hashes, rather than machine-specific filesystem locations, identify a corpus.
        value = value.model_dump(mode="json", exclude={"corpus"})
    elif isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)
    legacy_exclude: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def serialize_version(self, handler):
        # Explicit v1 projections preserve normalized historical JSON, including
        # nested defaults. exclude_unset/raw-JSON hashing would not do so.
        value = handler(self)
        if getattr(self, "schema_version", None) == 1:
            for name in self.legacy_exclude:
                value.pop(name, None)
        return value


class AgentMode(str, Enum):
    NO_MEMORY = "no-memory"
    INSIGHTS_ONLY = "insights-only"
    WIKITRACE = "wikitrace"


class Budget(Record):
    model_calls: Annotated[StrictInt, Field(ge=1)] = 20
    patch_attempts: Annotated[StrictInt, Field(ge=1)] = 3
    max_output_tokens: Annotated[StrictInt, Field(ge=128)] = 2048
    total_tokens: Annotated[StrictInt, Field(ge=128)] = 40000
    wall_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1200


class ModelSettings(Record):
    provider: Literal["openai"] = "openai"
    model: Annotated[str, Field(min_length=1)]
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None = 0
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None

    @model_validator(mode="after")
    def validate_sampling(self):
        if self.model.startswith("gpt-6") and self.reasoning_effort is None:
            raise ValueError("Set GPT-6 reasoning effort explicitly for a reproducible comparison")
        if self.reasoning_effort not in {None, "none"} and self.temperature is not None:
            raise ValueError("Omit temperature (use null) when reasoning effort is enabled")
        return self


class RetrievalSettings(Record):
    algorithm: Literal["bm25-v1"] = "bm25-v1"
    query: Literal["initial-failed-logs-and-steps-v1", "initial-failed-step-diagnostics-v2"] = "initial-failed-logs-and-steps-v1"
    tokenizer: Literal["casefold-identifiers-v1", "diagnostic-identifiers-v2"] = "casefold-identifiers-v1"
    k1: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.2
    b: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 0.75
    max_insights: Annotated[StrictInt, Field(ge=1)] = 4
    max_characters: Annotated[StrictInt, Field(ge=1)] = 8000
    max_per_training_task: Literal[1] = 1


class TaskIdentity(Record):
    id: Identifier
    family: Identifier
    split: Literal["train", "dev", "test"]
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")]
    base_sha: Revision


class TaskSpec(Record):
    legacy_exclude: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "artifact_prefix", "expected_oracle_test_count",
        "verification_contract_sha256", "oracle_contract_path",
    })
    schema_version: Literal[1, 2] = 1
    id: Identifier
    family: Identifier
    split: Literal["train", "dev", "test"]
    category: Literal["dependency", "import", "test", "configuration"]
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")]
    base_sha: Revision
    initial_run_url: str
    capture_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    capture_sha256: Digest | None = None
    workflow: str = "ci.yml"
    oracle_ref: str = "main"
    oracle_sha: Revision | None = None
    expected_test_count: Annotated[StrictInt, Field(ge=1)] = 36
    artifact_prefix: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")] = "invoice-ci"
    expected_oracle_test_count: Annotated[StrictInt, Field(ge=1)] | None = None
    verification_contract_sha256: Digest | None = None
    oracle_contract_path: str | None = None
    editable_paths: list[str] = Field(default_factory=lambda: ["src/**/*.py", "pyproject.toml", "ci/constraints.txt"])
    editable_environment_keys: list[str] = Field(default_factory=lambda: ["INVOICE_CURRENCY"])
    protected_paths: list[str] = Field(default_factory=lambda: ["tests/", ".github/", "ci/record_environment.py"])
    required_steps: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "invoice-checks": [
                "Check dependency consistency", "Run tests", "Build package", "Run example invoice"
            ]
        }
    )

    @model_validator(mode="after")
    def validate_reference(self):
        reference = RunReference.parse(self.initial_run_url)
        if reference.full_name != self.repository:
            raise ValueError("The run URL and task repository must match")
        if reference.attempt is None:
            raise ValueError("Pin the initial run URL to /attempts/N")
        if not self.editable_paths or not self.required_steps:
            raise ValueError("A task needs editable paths and required verification steps")
        if any(not steps for steps in self.required_steps.values()):
            raise ValueError("Each required job must declare verification steps")
        if self.schema_version == 1:
            if self.artifact_prefix != "invoice-ci" or any(value is not None for value in (
                self.expected_oracle_test_count, self.verification_contract_sha256, self.oracle_contract_path,
            )):
                raise ValueError("Generic verifier settings require task schema_version 2")
        else:
            explicit = {
                "expected_test_count", "artifact_prefix", "expected_oracle_test_count",
                "verification_contract_sha256", "oracle_contract_path", "editable_paths",
                "editable_environment_keys", "protected_paths", "required_steps",
            }
            if not explicit <= self.model_fields_set or any(value is None for value in (
                self.expected_oracle_test_count, self.verification_contract_sha256,
                self.oracle_contract_path, self.oracle_sha, self.capture_sha256,
            )):
                raise ValueError("V2 tasks need explicit generic boundaries, counts, contract, oracle, and capture hashes")
            if self.artifact_prefix == "invoice-ci" or self.editable_environment_keys:
                raise ValueError("V2 uses generic verification without editable workflow environment")
        return self


class ExperimentSpec(Record):
    legacy_exclude: ClassVar[frozenset[str]] = frozenset({
        "retrieval", "split_registry", "phase_manifest_sha256", "request_timeout_seconds", "context_policy", "execution_parallelism",
    })
    schema_version: Literal[1, 2] = 1
    id: Identifier
    data_kind: Literal["live", "synthetic"] = "live"
    model: ModelSettings
    budget: Budget = Field(default_factory=Budget)
    trials: Annotated[StrictInt, Field(ge=3)] = 3
    tasks: list[TaskSpec] = Field(min_length=1)
    corpus: Path | None = None
    corpus_sha256: Digest | None = None
    insights_sha256: Digest | None = None
    hold_out_families: StrictBool = True
    seed: StrictInt = 42
    retrieval: RetrievalSettings | None = None
    split_registry: list[TaskIdentity] = Field(default_factory=list)
    phase_manifest_sha256: Digest | None = None
    request_timeout_seconds: Annotated[float, Field(gt=0, le=600, allow_inf_nan=False)] = 120
    context_policy: Literal["append-only-v1"] = "append-only-v1"
    execution_parallelism: Annotated[StrictInt, Field(ge=1, le=3)] = 1

    @model_validator(mode="after")
    def validate_design(self):
        if self.trials % 2 == 0:
            raise ValueError("Use an odd number of reference trials for majority outcomes")
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("Task IDs must be unique")
        train = [task for task in self.tasks if task.split == "train"]
        test = [task for task in self.tasks if task.split == "test"]
        if {(task.repository, task.base_sha) for task in train} & {(task.repository, task.base_sha) for task in test}:
            raise ValueError("Training and test tasks must not share the same failing source snapshot")
        if self.hold_out_families and {task.family for task in train} & {task.family for task in test}:
            raise ValueError("The experiment requires held-out scenario families")
        memory_fields = (self.corpus, self.corpus_sha256, self.insights_sha256)
        if any(value is not None for value in memory_fields) and not all(value is not None for value in memory_fields):
            raise ValueError("A frozen corpus needs its path, corpus hash, and insights hash")
        if self.schema_version == 1:
            if self.retrieval is not None or self.split_registry or self.phase_manifest_sha256 is not None or self.request_timeout_seconds != 120 or self.execution_parallelism != 1:
                raise ValueError("Research controls require experiment schema_version 2")
            if any(task.schema_version != 1 for task in self.tasks):
                raise ValueError("V1 experiments require v1 tasks")
        else:
            if self.retrieval is None or not self.hold_out_families or any(task.schema_version != 2 for task in self.tasks):
                raise ValueError("V2 requires retrieval settings, generic tasks, and held-out families")
            from buildtrace.experiments.research import create_split_registry
            create_split_registry(self.tasks, self.split_registry)
        return self


class Decision(Record):
    action: Literal["investigate", "apply", "adapt", "reject", "patch", "stop"]
    target: str
    parameters: dict = Field(default_factory=dict)
    summary: str = ""

    @property
    def actionable_fingerprint(self) -> str:
        return fingerprint(self.model_dump(exclude={"summary"}))


class EvidenceCitation(Record):
    insight_id: str
    source_run_id: str
    layer: Literal["compilation", "raw"]
    document: str
    first_line: Annotated[StrictInt, Field(ge=1)]
    last_line: Annotated[StrictInt, Field(ge=1)]
    document_sha256: Digest

    @model_validator(mode="after")
    def validate_lines(self):
        if self.last_line < self.first_line:
            raise ValueError("Evidence line range must be ordered")
        return self


class EvidenceDecision(Record):
    legacy_exclude: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "read_ids", "before_action_id", "after_action_id", "contaminated",
        "material_effect", "adoption", "adoption_action_ids", "mechanism_reviewer",
    })
    schema_version: Literal[1, 2] = 1
    id: Identifier
    question: str
    trigger: Literal["missing-detail", "conflict", "failed-source", "environment-difference", "failed-fix", "uncertainty"]
    before: Decision
    citations: list[EvidenceCitation] = Field(min_length=1)
    after: Decision | None = None
    review: Literal["unreviewed", "confirmed-change", "confirmed-no-change", "uncertain"] = "unreviewed"
    reviewer: str | None = None
    read_ids: list[str] = Field(default_factory=list)
    before_action_id: str | None = None
    after_action_id: str | None = None
    contaminated: StrictBool = False
    material_effect: Literal["unreviewed", "change", "refinement", "none", "uncertain"] = "unreviewed"
    adoption: Literal["unreviewed", "adopted", "not-adopted", "uncertain"] = "unreviewed"
    adoption_action_ids: list[str] = Field(default_factory=list)
    mechanism_reviewer: str | None = None

    @model_validator(mode="after")
    def validate_review(self):
        if self.review != "unreviewed" and not self.reviewer:
            raise ValueError("Evidence-impact review requires an independent reviewer")
        if self.review.startswith("confirmed-") and self.after is None:
            raise ValueError("Confirmed impact requires a before/after record")
        if (self.material_effect != "unreviewed" or self.adoption != "unreviewed") and not self.mechanism_reviewer:
            raise ValueError("Materiality/adoption annotations require an independent mechanism reviewer")
        return self

    @property
    def changed(self) -> bool | None:
        if self.after is None:
            return None
        return self.before.actionable_fingerprint != self.after.actionable_fingerprint


class ChangedHunk(Record):
    id: str
    path: str
    added_lines: Count
    deleted_lines: Count
    diff: str
    classification: Literal["unreviewed", "necessary", "unnecessary", "uncertain"] = "unreviewed"
    reviewer: str | None = None

    @model_validator(mode="after")
    def validate_review(self):
        if self.classification != "unreviewed" and not self.reviewer:
            raise ValueError("Hunk classification requires an independent reviewer")
        return self


class Verification(Record):
    attempt: Annotated[StrictInt, Field(ge=1)]
    candidate_sha: Revision
    run_url: str
    oracle_run_url: str | None = None
    workflow_conclusion: str
    required_checks_passed: StrictBool
    protected_files_unchanged: StrictBool
    oracle_passed: StrictBool
    elapsed_seconds: Seconds
    capture_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    oracle_capture_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    details: str = ""

    @property
    def passed(self) -> bool:
        return (
            self.workflow_conclusion == "success"
            and self.required_checks_passed
            and self.protected_files_unchanged
            and self.oracle_passed
        )


class Timing(Record):
    wall_seconds: Seconds
    ci_wait_seconds: Seconds = 0
    approval_wait_seconds: Seconds = 0

    @model_validator(mode="after")
    def validate_breakdown(self):
        if self.ci_wait_seconds + self.approval_wait_seconds > self.wall_seconds + 0.01:
            raise ValueError("Timing components cannot exceed wall time")
        return self

    @property
    def active_seconds(self) -> float:
        return max(0.0, self.wall_seconds - self.ci_wait_seconds - self.approval_wait_seconds)


class ObservableAction(Record):
    id: Identifier
    sequence: Count
    kind: Literal["plan", "current-observation", "history-read", "patch"]
    decision: Decision | None = None
    paths: list[str] = Field(default_factory=list)
    verification_attempt: Annotated[StrictInt, Field(ge=1)] | None = None
    succeeded: StrictBool = True


class HistoricalRead(Record):
    id: Identifier
    sequence: Count
    insight_id: str
    question: str
    trigger: str
    layer: str
    document: str = ""
    status: Literal["delivered", "error"]
    offset: Count | None = None
    requested_limit: Count | None = None
    delivered_characters: Count = 0
    source_run_id: str | None = None
    document_sha256: Digest | None = None
    excerpt_sha256: Digest | None = None
    first_line: Annotated[StrictInt, Field(ge=1)] | None = None
    last_line: Annotated[StrictInt, Field(ge=1)] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_delivery(self):
        if self.status == "delivered":
            if self.error is not None or self.layer not in {"raw", "compilation"} or any(value is None for value in (
                self.offset, self.source_run_id, self.document_sha256, self.excerpt_sha256, self.first_line, self.last_line,
            )):
                raise ValueError("Delivered historical evidence needs exact source/excerpt identities")
            if self.last_line < self.first_line:
                raise ValueError("Delivered evidence line range must be ordered")
        elif not self.error:
            raise ValueError("An unsuccessful historical read needs its error")
        return self


class RunRecord(Record):
    legacy_exclude: ClassVar[frozenset[str]] = frozenset({
        "split", "phase_manifest_sha256", "packet_sha256", "packet_insight_ids", "packet_query_sha256",
        "actions", "evidence_reads", "patch_attempts",
    })
    schema_version: Literal[1, 2] = 1
    id: Identifier
    experiment_id: Identifier
    experiment_sha256: Digest
    protocol_sha256: Digest
    task_id: Identifier
    task_sha256: Digest
    mode: AgentMode
    trial: Count
    role: Literal["reference", "rerun"] = "reference"
    model: ModelSettings
    budget: Budget
    corpus_sha256: Digest | None = None
    insights_sha256: Digest | None = None
    outcome: Literal["solved", "unsolved", "infra-error", "not-reproduced"]
    stop_reason: str
    timing: Timing
    model_calls: Count = 0
    input_tokens: Count = 0
    output_tokens: Count = 0
    reasoning_tokens: Count = 0
    cached_input_tokens: Count = 0
    returned_models: list[str] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    hunks: list[ChangedHunk] = Field(default_factory=list)
    evidence_decisions: list[EvidenceDecision] = Field(default_factory=list)
    trajectory_path: str
    split: Literal["train", "dev", "test"] | None = None
    phase_manifest_sha256: Digest | None = None
    packet_sha256: Digest | None = None
    packet_insight_ids: list[str] = Field(default_factory=list)
    packet_query_sha256: Digest | None = None
    actions: list[ObservableAction] = Field(default_factory=list)
    evidence_reads: list[HistoricalRead] = Field(default_factory=list)
    patch_attempts: Count = 0

    @model_validator(mode="after")
    def validate_outcome_and_access(self):
        if self.outcome == "solved" and (not self.verifications or not self.verifications[-1].passed):
            raise ValueError("A solved outcome requires a passing final verification")
        if self.outcome != "solved" and self.verifications and self.verifications[-1].passed:
            raise ValueError("A passing final verification must be recorded as solved")
        if self.mode == AgentMode.NO_MEMORY and (self.corpus_sha256 or self.insights_sha256):
            raise ValueError("No-memory runs must not receive historical memory")
        if self.mode != AgentMode.NO_MEMORY and (not self.corpus_sha256 or not self.insights_sha256):
            raise ValueError("Memory-enabled runs require a frozen corpus and insights hash")
        if self.mode != AgentMode.WIKITRACE and self.evidence_decisions:
            raise ValueError("Only WikiTrace runs can access historical source evidence")
        if self.role == "rerun" and self.mode != AgentMode.NO_MEMORY:
            raise ValueError("The rerun noise reference is a no-memory run")
        if self.model_calls > self.budget.model_calls or len(self.verifications) > self.budget.patch_attempts:
            raise ValueError("Recorded calls or patch attempts exceed the declared budget")
        if self.reasoning_tokens > self.output_tokens or self.cached_input_tokens > self.input_tokens:
            raise ValueError("Token breakdown cannot exceed the corresponding total")
        if len({hunk.id for hunk in self.hunks}) != len(self.hunks):
            raise ValueError("Changed-hunk IDs must be unique within a run")
        if self.schema_version == 2:
            if self.split is None or self.phase_manifest_sha256 is None:
                raise ValueError("V2 records need their executed split and frozen phase identity")
            if self.mode == AgentMode.NO_MEMORY and (self.packet_sha256 or self.packet_insight_ids or self.packet_query_sha256):
                raise ValueError("No-memory records cannot receive an insight packet")
            if self.mode != AgentMode.NO_MEMORY and (self.packet_sha256 is None or self.packet_query_sha256 is None):
                raise ValueError("Memory records need their actual injected packet identity")
            if self.mode != AgentMode.WIKITRACE and self.evidence_reads:
                raise ValueError("Historical reads are restricted to WikiTrace")
            if not len(self.verifications) <= self.patch_attempts <= self.budget.patch_attempts:
                raise ValueError("Candidate submissions and completed verification counts exceed their bounds")
            if len({action.id for action in self.actions}) != len(self.actions) or len({action.sequence for action in self.actions}) != len(self.actions):
                raise ValueError("Observable actions need unique chronological identities")
        return self

    @property
    def success(self) -> bool:
        return self.outcome == "solved"
