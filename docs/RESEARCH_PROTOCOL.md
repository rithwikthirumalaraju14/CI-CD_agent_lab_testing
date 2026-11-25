# Research-v2 operator protocol

`buildtrace.experiments.research` supplies separate v2 input identities, split
controls, a replay-based development gate, and review exports. The coordinator
connects these utilities to v2 schemas, journals, scheduling, and resume/report
validation. The existing v1 functions retain their original meaning.

The utilities perform local reads and return JSON dictionaries. They do not load
environment files, resolve credentials, call models/services, publish, or write
files. Models accepted by the APIs are converted with `model_dump(mode="json")`.

## Artifacts and the freeze sequence

Each artifact has `schema_version: 2`, a `kind`, and a `sha256`. Its address is
SHA-256 of canonical UTF-8 JSON, with sorted keys, compact separators, no NaN or
infinities, and **only its own root `sha256` field omitted**. Nested addresses
remain included. `content_sha256(value)` exposes that canonical JSON hash;
document/excerpt hashes instead hash bytes directly.

| Artifact / creator | Meaning |
| --- | --- |
| `research-split-registry` / `create_split_registry` | Cross-spec assignment of task identities to train/dev/test. |
| `research-input-manifest` / `create_input_manifest` | A training, development, or evaluation **candidate**, with an explicit runtime closure and active split inputs. |
| `research-development-gate` / `evaluate_development_gate` | Replayed development evidence, coverage, exclusions, and a `passed` result. |
| `research-protocol-freeze` / `freeze_protocol` | Current inputs reverified and the development gate recomputed successfully. |
| `research-evaluation-freeze` / `freeze_evaluation` | One held-out test candidate bound to the approved, identical protocol. |

The dependency graph is deliberately one-way:

```text
runtime + model/budget/retrieval + corpus/training ledger + seed policy
                                |
                  development input candidate
                                |
                  dev records + independent reviews
                                |
                       development gate
                                |
                        protocol freeze
                                |
               held-out input candidate -> evaluation freeze
                                                  |
                                             test records
```

`spec.phase_manifest_sha256` is excluded from candidate construction. Development
records use the **development candidate's** `sha256` in this field. Test records
use the **evaluation freeze's** `sha256`. `run.protocol_sha256` is the candidate's
`protocol_sha256`, not the v1 runner's source-only fingerprint. These identities
must also be checked when resuming.

There is no result-dependent test-set tuning step. Changing effective code,
prompts, dependencies, retrieval, budgets, model, corpus, or seed policy changes
the protocol and requires a new development approval.

## Cross-spec split registry

```python
registry = create_split_registry(tasks, registry=previous_registry)
verify_split_registry(registry)  # returns its checked sha256
```

An identity is `{id, split, repository, base_sha, family}`. The registry checks
**every pair** of train/dev/test splits independently for overlapping IDs,
repository/full-base-SHA pairs, and specific families. Repository names and
families are case-normalized; ID collisions are checked case-insensitively.
Exact identities, source snapshots, and families may recur within the same
split. Rebinding an existing ID to another snapshot/family is rejected.

`registry` may be the sealed dictionary or a list of identities. The planned
`spec.split_registry` field is a list. Candidate construction merges that list,
all declared task identities, the supplied prior registry, and actual corpus
training identities. Reuse the persisted registry across separate experiment
specs: a new spec alone cannot discover an omitted previous assignment.

Specific family labels describe the concrete failure mechanism, such as quoted
record boundaries or distribution-resource lookup. Broad tags such as
`configuration` are not held-out mechanisms. The gate rejects several coarse
labels, but meaningful family assignment still requires operator review; a
different spelling does not establish conceptual independence.

## Candidate inputs and effective source

```python
candidate = create_input_manifest(
    spec, "dev", memory=memory, registry=registry,
    gate_task_ids=["dev-parser", "dev-resource"],
    extra_files=[
        "infra/research_v2/verify.py",
        "infra/research_v2/trusted-ci.yml",
        "infra/research_v2/pytest-trusted.ini",
    ],
)
```

The spec must declare `schema_version: 2`, a nonempty `retrieval` object, model,
common budget, trials, seed, and tasks. An active task needs its capture ID/hash,
full trusted `oracle_sha`, and positive original `expected_test_count` pinned
before candidate creation. The complete active task dump, including verifier
contract fields, is hashed. Capture-byte/proof validation belongs in the
coordinator's archive/backend preflight; an ID/hash declaration alone does not
prove a valid CI run.

The manifest includes:

- `protocol.runtime`: all `experiments/*.py`, `buildtrace/{__init__,evidence,
  github,models}.py`, statically imported local Python modules/parent packages,
  `pyproject.toml`, and explicit extra runtime/control files. Dirty and untracked
  Python files in the experiments directory are included. Git status, Git HEAD,
  timestamps, and absolute source locations are not identities.
- Exact installed dependency versions, VCS commit identities where present,
  transitive installed runtime requirements, and hashes of effective package
  source/binaries/templates/resources. This detects editable/local source
  changes at the same version. Requirements ranges in `pyproject.toml` are not
  treated as exact installed pins. Installed conditional requirements are
  conservatively included; missing conditional requirements are recorded.
- `protocol.runtime.interfaces`: `shared_prompts_sha256`, individual exported
  prompt-constant hashes, `shared_tools_sha256`, and per-mode tool-definition
  hashes. These are computed from the effective prompt module's exports and
  tool-definition return values, in addition to hashing its source file.
- Model/settings, common budget, exact retrieval/selector configuration, other
  declared protocol controls, gate-policy hash, and deterministic seed policy.
- Source corpus/insights hashes, the actual training-session ledger and its hash,
  upstream/compiler metadata, insight-to-training-session links, and corpus file
  hashes. Corpus bytes are verified locally during candidate creation.
- `active.tasks`, `active.captures`, their separate hashes, the concrete active
  schedule/hash, and predeclared `active.gate_task_ids` on development candidates.

Development and test require the real frozen memory object. It must expose its
`manifest` and either `directory` or `read_source(relative) -> bytes`; existing
`FrozenMemory` works via `directory`. Training may have no corpus. Existing
`training_tasks` ledgers get source-step names by their original ordered position
(`step-0000`, etc.); an explicit `source_step` is also supported.

Only active-split task/capture/verifier **contents** enter `active`. Other split
identities still enter the registry. Editing an inactive capture/verifier does
not change a development candidate; changing a registered identity does. The
protocol portion excludes task/capture manifests and the split registry, so a
new held-out identity set can be frozen after development without changing the
approved solver protocol.

The hosted operator explicitly declares `execution_parallelism: 3`. Execution
uses fixed consecutive schedule blocks: all three modes of a task/repeat run
together; training uses the same ceiling for its three independent repetitions.
Each trial has separate conversation/backend/state, and one coordinator writes
the aggregate in seeded schedule order. No next block starts until the current
block completes. Parallelism is a protocol control, identical in dev and test;
infrastructure failures remain recorded for the whole block. The legacy default
is serial execution. Foreground checkpoints can resume completed cells without
replacing them.

These are shared-project snapshots: a genuinely observed training source file
can contain healthy implementations of mechanisms later broken in another
snapshot. This is not source-code-, project-, or broad-concept-held-out transfer.

Extra files must describe the effective runtime actually used, including remote
verifier artifacts archived by the coordinator. Dynamic imports, external
drivers, workflow files, prompt resources outside the dependency packages, and
additional verifier control files need explicit entries when applicable. An
external file is expressed as `{portable_label: local_path}`; only the label and
bytes enter the identity. Reverification needs the same explicit closure.

`verify_input_manifest(candidate, spec, memory=..., registry=...,
extra_files=...)` recomputes the closure and returns its checked address or raises
`ResearchControlError`. Use it before execution/resume and before final freezing.
Run freezes from a fresh interpreter after source edits so loaded Python code
matches the recorded on-disk implementation. `source_root` is an optional
explicit checkout root for staged/local verification; it is not stored.

Machine-local corpus locations and `phase_manifest_sha256` are excluded from
candidate identities. Other absolute paths and credential-shaped protocol fields
are rejected. Environment files are excluded/rejected before source reads.
Dependency origin URLs omit userinfo/query/fragment and editable checkout paths.

## Development mechanism gate

Predeclare `gate_task_ids` **before development runs** and persist the candidate.
Omitting the argument selects all development tasks. Select at least two
applicable tasks from distinct specific families; unrelated dev tasks need not
read historical sources. The full reference grid for **all active dev tasks** is
three modes × exactly three repeats. No cells, failed attempts, or infrastructure
outcomes are selectively dropped. `seed_schedule(spec, split)` supplies the v2
schedule: seeded task/mode shuffles and cyclic mode rotations; both dev and test
have all modes, and training has no-memory only.

```python
gate = evaluate_development_gate(
    spec, reviewed_records, manifest=candidate, memory=memory,
    independent_reviewers=["independent-mechanism-reviewer"],
    solver_identities=["solver-agent"],
)
```

The gate checks matched split/phase/protocol/task/model/budget/corpus identities,
one record per scheduled cell, consistent served model versions, and identical
selected packet hashes/ordered insight IDs across both memory modes and repeats
on each task. No-memory receives no packet/corpus; source access is WikiTrace-only.
Packet delivery itself is recorded by the integrated runner; packet hash equality
alone is not independent proof of prompt injection.

Each predeclared task needs qualifying chains in **at least two different
WikiTrace repeats**. Each chain must have all of:

1. Successful, nonblank delivered excerpts linked from the selected insight
   packet to the frozen training source. Full document bytes, excerpt bytes,
   character offset/count, and first/last lines must replay exactly. Raw bytes
   are decoded as UTF-8 with replacement, matching `FrozenMemory`; excerpt
   SHA-256 hashes the resulting excerpt encoded as UTF-8. Offsets count Python
   characters, not bytes. Line endpoints follow the existing reader, including
   the increment for a delivered trailing newline.
2. At least one **raw original observation**, not merely metadata or a model's
   interpretation. Eligible capture-linked observations are original log,
   source, or artifact entries whose capture manifest and entry hash replay.
   Initial capture revisions must match the training ledger. Capture manifests,
   `metadata.md`, `verification.json`, `verification-proof.json`, and
   `test-execution.json` alone do not qualify. Alternatively, the original
   source-step `solver.full.jsonl` can qualify when the delivered range overlaps
   the nonempty successful `read_current_file`/`read_current_log` **result
   payload**, not just its header, arguments, assistant notes, or verification
   outcome. Compilation/metadata navigation may accompany an actual raw
   observation; every claimed delivery in that burst must still replay.
3. Fresh, matching before/after plan actions surrounding exactly the recorded
   history-read burst. An intervening current observation, patch, extra plan, or
   unlisted read invalidates attribution even if `contaminated` says false.
4. An allowlisted independent reviewer labels the effect `change` or
   `refinement` and adoption `adopted`. The actionable decision (`action`,
   `target`, `parameters`) must actually differ. Summary-only rewording does not
   count. Reviewer names matching declared solver/agent/model identities are
   excluded. An allowlist is an operator attestation of independence, not proof
   that two real people/models are independent.
5. Named subsequent observable actions or patches adopt the exact reviewed plan
   snapshot and operate on its explicit path target or `path`, `file`, `paths`,
   or `files` parameters. Plan statements alone, nonexistent action IDs,
   unrelated paths, or a new intervening plan/history burst do not count.

At least one qualifying chain must end in a patch linked to the final passing
verification and `outcome: solved`. That patch must be the last patch action;
two patch actions cannot claim the same verification attempt. The verification
requires successful target checks, unchanged protected files, a passing oracle,
and an exact candidate SHA.
The archive/backend verifier remains responsible for the underlying CI proof.
Other qualifying chains may be unsuccessful repairs. There is **no score
superiority requirement**, and reading every task/repeat is not required.

The gate is read-only and does not modify records. A spec/candidate or structural
input mismatch raises `ResearchControlError`. An unmet grid/mechanism requirement
returns `passed: false` and `failures`. `tasks[id]` contains `qualifying_trials`,
qualified `chains` with replayed identities, and `rejected_chains` with reasons;
the report also retains outcome counts, reviewer identities, and a records hash.
Errors and contaminated/unadopted chains never count toward coverage. An
unneeded third repeat may contain a rejected chain while the other two qualify.

For a read-only source adapter, omit memory and pass
`source_resolver(relative_corpus_path) -> original_bytes`. The candidate already
binds the corpus file ledger and insight links. The adapter must serve that
frozen corpus locally. Source resolver errors become rejected chains. This is
the gate's only external input beyond its supplied data; it performs no writes.

### Exact v2 journal contract

In addition to the planned v2 fields, integration needs these conventions:

- `evidence_reads[i].id` **equals** its `actions[k].id`, and their `sequence`
  values match. All action IDs and action sequences are unique within a run.
  Log every historical request, including errors. A delivery records the planned
  `status`, hashes, offset/count, line endpoints, question, and trigger.
- A read's `document` may be source-step-relative, corpus-relative, or prefixed
  with the existing virtual `/work/corpus/`. Other absolute paths are invalid.
  Citations use corpus-relative or `/work/corpus/` paths. Every citation must
  correspond to a delivered read, and every delivered read needs a citation.
- A burst's ordered `read_ids` lists all history actions between
  `before_action_id` and `after_action_id`. The event's `before`/`after` exactly
  match those plan-action decisions. Its question/trigger match the first read.
  Read IDs cannot be reused to attribute multiple bursts.
- Record the active `decision` snapshot on adoption actions, not only plans.
  Observable adoption actions have nonempty relative `paths`; patch actions have
  a `verification_attempt` linking to a unique sequential verification record.
  Successful current observations can represent adopted investigation actions.
- Review annotations are `contaminated: false`,
  `material_effect: "change" | "refinement"`, `adoption: "adopted"`,
  `adoption_action_ids: [...]`, and `mechanism_reviewer`. Existing legacy review
  flags alone cannot satisfy the gate. Independent review judges actual
  materiality and implementation/adoption; structural replay does not establish
  semantic correctness or causal benefit.

## Freezing and resume checks

```python
approved = freeze_protocol(
    dev_spec, reviewed_records, manifest=dev_candidate, memory=memory,
    registry=registry, extra_files=runtime_extras,
    independent_reviewers=mechanism_reviewers,
)
# freeze_protocol recomputes the gate; it does not accept a caller's passed flag.
test_candidate = create_input_manifest(
    test_spec, "test", memory=memory, registry=registry,
    extra_files=runtime_extras,
)
evaluation = freeze_evaluation(
    test_spec, manifest=test_candidate, protocol_freeze=approved,
    memory=memory, registry=registry, extra_files=runtime_extras,
)
# Store evaluation["sha256"] as the test spec/run phase_manifest_sha256.
verify_input_manifest(
    test_candidate, test_spec, memory=memory, registry=registry,
    extra_files=runtime_extras,
)
verify_freeze(
    evaluation, test_candidate, protocol_freeze=approved,
    spec=test_spec, records=completed_test_records,
)
```

`verify_freeze` returns the checked freeze SHA. It checks identities on partial
test records for resume; the coordinator's final reporting additionally requires
the complete test grid. Supplying dev records checks the full frozen reviewed
cohort hash. Record commitments exclude the machine-local `trajectory_path`;
recorded observations/actions/reviews are included. `verify_input_manifest` is
the separate current-byte revalidation step.

Persist the candidate, registry, original results, reviewed derived results,
gate, protocol freeze, test candidate, and evaluation freeze separately. Content
addresses detect changes relative to a retained expected address; they are not
signatures or evidence of chronology when an operator replaces every artifact.

## Separate hunk and evidence reviews

```python
hunks, evidence, private = create_review_packages(
    original_records, original_results_bytes,
    task_context=neutral_task_requirements,
    manifest=candidate, memory=memory,  # optional exact excerpt replay
)
```

The helper verifies records against the original JSONL bytes and returns **three
separate dictionaries**:

- `research-hunk-review`: randomly ordered items with unpredictable `h-...` IDs,
  path/diff/line counts, optional neutral requirements, and blank hunk-review
  fields. It contains no run/task/hunk IDs, mode, outcomes, evidence, or shared
  results-file hash.
- `research-evidence-review`: independently randomized `e-...` IDs, questions,
  before/after decisions, citations, reads, observable actions, final hunks,
  verification records, and blank mechanism-review fields. Supplying the
  candidate and memory/resolver adds replayed excerpts/identities or explicit
  replay errors. Without them it is a journal-data export requiring the corpus
  for source review.
- `research-review-mapping`: **private** token-to-run/hunk/event maps, original
  results-byte hash, records hash, and both template-package hashes.

Provide only the hunk package and neutral task requirements during blinded hunk
review. Lock that review before exposing evidence or the private map. Tokens,
order, and public headers cannot link a hunk to an evidence run. Patch contents
and reviewer knowledge can still provide semantic clues; this is identifier
blinding, not guaranteed perfect blinding or a claim of a human panel.

Attach annotations to a derived copy using the private mapping and retain the
originals/template hashes. The coordinator owns that attachment step. Do not
reuse hunk tokens in evidence forms or publish the private mapping to the hunk
reviewer during annotation.

## Offline verification

```text
.venv/Scripts/python.exe -m pytest tests/test_research_controls.py -q
```

The tests include successful mechanisms with worse WikiTrace scores, unread
inapplicable dev tasks, cross-spec leakage, dirty/untracked source changes,
editable dependency changes, excerpt/capture/journal tampering, failed reads,
contamination, fabricated adoption, incomplete grids, post-development tuning,
freeze identity mismatches, and review-linkage privacy. They are synthetic
control checks, not measured repair-agent results.
