# Foreground hosted development operations

Entry point: `python -m buildtrace.research_benchmark.operate` from the operator
checkout. This driver prepares authored cases, publishes immutable snapshots,
captures their real failing CI attempts, writes strict schema-2 specifications,
and performs reference-repair infrastructure smokes. It does not call a model,
compile memory, create model trials, or admit its smoke records as histories.

Common options, accepted before or after the command:

- `--root`: defaults to `data/research-v2`.
- `--repository`: defaults to
  `bluepal-rithwik-thirumalaraju/buildtrace-ci-lab`.
- `--split train|dev|test`: required on every command; `development` aliases `dev`.
- `--case rv2-001`: optional, repeatable exact-ID filter within the active split.
  A case from another split is rejected before any preparation/network work.
- `--allow-publish`: required for `publish` and `smoke`.

`prepare` and `spec` are entirely local and do not load credentials. Invoked
network commands may load dotenv without overriding existing environment values,
then use the existing environment/gh/explicit Git credential-account resolver.
Console output consists of short counts, opaque refs, run URLs and sanitized
error summaries. Tokens, HTTP response bodies and request headers are not printed.

## One training case first

Run these commands in order, in the foreground, from the repository root:

```bash
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate prepare --split train --case rv2-001
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate publish --split train --case rv2-001 --allow-publish
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate capture --split train --case rv2-001 --wait-seconds 1200 --poll-seconds 10
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate smoke --split train --case rv2-001 --allow-publish --timeout-seconds 3600
```

The lab already has `.github/workflows/ci.yml` on its default branch for dispatch
availability. These operations do not edit that branch. Publishing the input ref
triggers the initial push workflow; oracle refs and backend trial refs do not
trigger that push filter.

If capture returns `pending`, rerun the **same** capture command. A short
checkpoint/poll invocation is also available:

```bash
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate capture --split train --case rv2-001 --wait-seconds 0
```

After a real failed input capture, the optional one-case specification is:

```bash
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate spec --split train --case rv2-001 --id research-v2-train-one --output "data/research-v2/specs/train-one.json"
```

Specification output paths must be new. Each specification contains exactly the
selected captured cases; missing captures are errors, not silently dropped tasks.

## Training batch

Omitting `--case` selects all 12 training cases. The already completed first case
is resumed after validation, rather than overwritten or republished:

```bash
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate prepare --split train
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate publish --split train --allow-publish
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate capture --split train --wait-seconds 1200
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate smoke --split train --allow-publish --timeout-seconds 3600
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate spec --split train --id research-v2-training-v1 --output "data/research-v2/specs/train-v1.json"
```

The operator smokes test reference solutions and hosted verification. Actual
no-memory training sessions and the compilation of their complete trajectories
use the coordinator's experiment workflow. Smoke files must not be supplied to
that compiler.

## Development batch

These commands select only the six development cases:

```bash
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate prepare --split dev
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate publish --split dev --allow-publish
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate capture --split dev --wait-seconds 1200
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate smoke --split dev --allow-publish --timeout-seconds 3600
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate spec --split dev --id research-v2-development-v1 --output "data/research-v2/specs/dev-v1.json" --corpus "data/research-v2/memory/train-v1"
```

The last command assumes the coordinator has created a genuine `FrozenMemory`
corpus at the given path from separate training sessions. The driver verifies
its content/insight hashes and model/split separation. `--corpus` is optional
when constructing a specification, but the three-mode development runner itself
requires real frozen training memory. None is synthesized here.

Train/dev commands never prepare, hash, publish, capture or smoke held-out project
trees. They carry previously registered held-out **identity metadata** in their
cross-split registry. Held-out operations require explicitly selecting `--split
test`; creation of a test spec does not supply the separate development gate or
approved evaluation freeze required by the experiment runner.

## Files and trust boundaries

The default local layout is:

```text
data/research-v2/
  cases/<case-id>/
    raw/                  original broken standalone project
    bundle/               separate generator oracle bundle/manifest
    public/               ONLY this tree becomes the input branch
    trusted/              ONLY this tree becomes the oracle branch
    prepared.json         sealed operator plan, hashes, refs and task fields
    journal/000001.json   append-only hash-chained checkpoints
  blob-cache/<repo-hash>/<git-blob-sha>.json
  identities/<case-id>.json
  registries/<registry-hash>.json
  evidence/captures/<capture-id>/
  tasks/by-id/<case-id>.json
  tasks/<split>-<content-hash>.json
  specs/<new-output-name>.json
  smokes/operator-smoke-<random-id>/
    intent.json           complete operator-only reference repair
    pinned-task.json
    journal/000001.json   mutation intents/status/object IDs, no HTTP bodies
    result.json           raw structured verification/hunks/observations/error
```

Preparation invokes the generator and imports
`infra/research_v2/prepare_hosted.py` through `importlib`. It stages new raw/bundle
and public/trusted trees, checks their exact file sets and separate contract,
then publishes missing local directories. Existing directories are reused only
when every file hash and tree fingerprint agrees. Existing contents are never
overlaid. Interrupted preparation can recover already moved identical leaves.
Changed generator/hosted inputs require a new root; frozen trees are not rewritten.
Preparation also tolerates transient local `PermissionError` locks with four
bounded waits totaling 0.75 seconds. A retry reconciles the same case and verifies
already published leaves; it cannot change bytes or overwrite existing metadata.

All operator JSON, absolute local paths, family/split labels, mutation recipes and
reference repairs remain outside both deploy trees. The public descriptor contains
only its schema and public test count. The trusted branch has its canonical
contract and only hash-declared oracle tests/artifacts, without the generator's
operator manifest.

Refs are deterministic hashes of the repository plus the two complete Git trees:

```text
buildtrace-inputs/rv2-<24-hex>
buildtrace-oracles/rv2-<24-hex>
```

Commit messages contain only `Benchmark snapshot <opaque-id>`. Input/oracle
commits are orphan commits with fixed author/committer metadata, permitting
read-only reconciliation of a lost POST response by its expected Git SHA. File
blobs use base64 of the exact bytes, including local wheels and compressed data.
New trees have no `base_tree`, so they cannot inherit files/history from the lab
default branch. Identical confirmed blob IDs are cached across cases.

An existing ref is adopted only if its orphan commit and complete, untruncated
regular-file tree match the intended snapshot. Once recorded, its SHA is pinned;
even a same-tree ref movement is rejected. No ref is moved, force-updated or
deleted. The driver does not invoke local Git commit/push commands.

## Durable checkpoints and failure behavior

Use one foreground writer per root. JSON checkpoints are immutable, hash checked,
and atomically linked into place; publication intent is saved **before** each
mutation, and confirmed object/ref identities after durable responses. The shared
client spaces mutation requests by approximately 1.05 seconds, including backend
smoke mutations. GETs use the existing bounded GitHub retry helper. POSTs are sent
once, without automatic transport/status retries.

An ambiguous mutation is reconciled by GET of the exact object/ref. If it remains
unconfirmed, the command stops and a subsequent invocation performs reads rather
than blindly repeating its POST. A definite HTTP rejection can be attempted again
by a later foreground invocation after resolving its cause. The journal retains
the failure either way. Do not delete these checkpoints to manufacture a success.

The registry contains all published/captured case identities, regardless of the
active case filter or split. It also conservatively registers a real confirmed
input commit before its ref POST, so an interruption after ref acceptance cannot
hide that identity from another split's specification. Pending publication is
still distinguishable in the case journal. Registry records contain only ID,
split, specific family, repository and base SHA; cross-split ID/snapshot/family
reuse is rejected by the shared research controls.

Capture searches bounded push-run listings by the exact input ref, base SHA and
workflow path, refuses multiple matching run IDs, then pins **attempt 1** of the
unique push run. It never selects a rerun based on outcome. It archives the exact
attempt through `GitHubActionsClient` and `EvidenceStore`, including original log
ZIP bytes and readable log entries, and the original public artifact when present
and downloadable. Only a completed `failure`, with the exact identity, successful
bootstrap steps and an actual failed project step, becomes a `TaskSpec`.

Unexpected success, cancellation/timeouts, bootstrap/upload failures and identity
mismatches are archived and remain nonadmitted. Missing original logs preserve
the incomplete capture; a later invocation may collect the **same pinned attempt**
again to recover transport/log availability without erasing the earlier archive.
The driver does not manufacture URLs, CI outcomes or task captures. Admission
checks are conservative structural checks of real CI metadata/log availability,
not a claim to diagnose every possible infrastructure failure inside a project
command; inspect any anomalous failure before freezing a study.

The per-case task is validated through strict `TaskSpec` schema 2, including both
test counts, canonical deployed contract hash, oracle ref/SHA, original capture
hash, editable/protected paths, empty editable-environment list, artifact prefix
and generic required steps. `spec` carries the entire registry, while selecting
only the active split/cases as executable tasks. It explicitly sets:

- `gpt-6-luna`, reasoning `medium`, temperature `null`;
- 60 calls, 5 patches, 16,384 output tokens/call, 4,000,000 cumulative tokens,
  3,600 wall seconds, request timeout 120 seconds;
- 3 trials, seed 42, schema 2 and held-out families. Newly generated specs use the
  development-revised `initial-failed-step-diagnostics-v2` query and
  `diagnostic-identifiers-v2` tokenizer, with the original BM25 constants and
  packet limits. Existing specs keep their explicitly serialized earlier rules.
- `execution_parallelism: 3`, fixed balanced schedule blocks, with isolated trial
  conversations/backends and one aggregate writer; identical in development/test.

When consuming these specifications, point the coordinator's evidence store to
`EvidenceStore(<root>/evidence)` (experiment CLI: `--data-dir
data/research-v2/evidence` for the default root). Capture IDs refer to that store,
not the earlier pilot's data directory.

`smoke` applies exact reference inverses only to allowed application source,
pyproject or constraints, passing complete file contents to a fresh
`GitHubTrialBackend` namespace. It calls `pin_oracle()` and `submit_patch()` and
saves the backend's structured result. On `BackendError`, `as_dict()`, partial
verification, hunks, capture IDs, CI timing and observations are preserved;
authentication material is redacted. Every smoke is explicitly marked
`model_trial=false` and `admit_to_memory=false`.

A completed smoke is reused on restart; a nonpassing result stays nonpassing.
An interrupted smoke is not automatically replayed. After inspecting its original
journal/captures, an explicit `--new-smoke` requests a fresh namespace while
retaining the old operation, for example:

```bash
".venv/Scripts/python.exe" -m buildtrace.research_benchmark.operate smoke --split train --case rv2-001 --allow-publish --new-smoke
```

Exit codes: **0** completed/passing; **1** pending/rejected capture or nonpassing
smoke; **2** invalid input, boundary/hash conflict, transport/checkpoint failure,
or unresolved publication. Keyboard interruption leaves checkpoints for resumption.

## Local operator tests

```bash
".venv/Scripts/python.exe" -m pytest -q tests/test_research_operator.py --basetemp "C:/Users/HP12D5~1/AppData/Local/Temp/opencode/research-operator-tests"
```

These tests use only mocked GitHub transport and an existing `.venv`. They forbid
real HTTP and dotenv reads, and cover byte preservation, separation, immutable
refs, partial publication, capture honesty, schema/count contracts, registry
coverage, reference-smoke error preservation and train/dev isolation. They do
not rerun the generator/build matrix or constitute a hosted validation result.
