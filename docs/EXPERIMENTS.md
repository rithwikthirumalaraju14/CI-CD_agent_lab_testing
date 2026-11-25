# Three-agent CI repair experiment

This page documents the **schema-1 invoice pilot**. The broader schema-2 workflow
uses selected task-specific packets, identical strategic guidance, separate
train/dev/test registries, a replayed development gate and explicit freezes; see
[RESEARCH_PROTOCOL.md](RESEARCH_PROTOCOL.md) and
[RESEARCH_V2_RESULTS.md](../RESEARCH_V2_RESULTS.md). Two development candidates
are complete; both failed the original-source gate and no new held-out comparison
was started. The first pilot's records and report semantics remain replayable.

## Research question

Does checking the source evidence behind historical debugging lessons improve
successful repairs and reduce memory-induced regression on Python CI failures?

The comparison is the main deliverable. The existing viewer supplies auditable
execution evidence to that experiment.

## The three modes

| Mode | Current project tools | Historical insights | Historical compilations and raw sessions |
| --- | --- | --- | --- |
| `no-memory` | Yes | No | No |
| `insights-only` | Yes | Yes | No |
| `wikitrace` | Yes | The exact same text | Read-only, source-linked access |

All modes run `run_trial` in `src/buildtrace/experiments/runner.py`, with the same
OpenAI model, reasoning effort, tool-result limits, repair budgets, and verifier.
Historical access is enforced by the dispatcher as well as the tool list. A
forged historical tool call from either baseline is rejected.

Both memory-enabled modes receive every insight from one frozen corpus. They
also receive identical source outcomes and applicability text, making the
insights-only baseline fully informed about those metadata. WikiTrace adds the
source-reading tool and CI-adapted selective-descent guidance.

The implementation directly uses the official WikiTrace compiler and corpus
renderers at a pinned commit. See [upstream attribution](../THIRD_PARTY.md).

## Current pilot inputs

The lab now has four training cases and four held-out test cases, one per failure
category in each split. Their branches use neutral IDs. Scenario recipes and
reference repairs remain in the operator's [catalog](LAB_SCENARIOS.md), outside
the project context supplied to the agent.

- Training configuration: [`experiments/training.json`](../experiments/training.json).
- Evaluation configuration: [`experiments/evaluation.json`](../experiments/evaluation.json).
- Model: [`gpt-6-luna`, medium reasoning](MODEL_SELECTION.md).
- Three reference trials per task and mode.
- Training: 4 tasks x 3 no-memory runs = 12 source sessions.
- Evaluation: 4 tasks x 3 modes x 3 trials = 36 repair trials.
- Optional noise reference: 4 tasks x 3 additional no-memory runs = 12 trials.

These are controlled, intentionally introduced bugs with real GitHub Actions
failures. The first live pilot is now complete; see [PILOT_RESULTS.md](../PILOT_RESULTS.md)
for the scored evaluation-v2 cohort, retained infrastructure outcomes, the
11-session admitted training corpus, and the preserved incomplete earlier work.
Synthetic responses are used only in tests and their reports are explicitly labeled.

The pilot checks the end-to-end method. Four held-out cases provide limited
coverage; expand to the planned larger held-out benchmark before making broad
claims about reliability.

## Verification contract

Each proposed patch creates a new trial branch from its fixed failing revision.
The runner requires both:

1. A new execution of the candidate's own CI, with every declared required step
   actually passing.
2. A separate trusted workflow execution from a pinned healthy revision against
   that candidate. Its original JUnit report must contain all 36 tests, with
   zero failures, errors, or skips. Its environment artifact must prove the
   checked-out candidate SHA.

The trusted workflow fixes test discovery and clears extra pytest options.
Tests and the environment recorder are protected. Configuration cases allow
declared environment-setting repairs while rejecting changes to CI commands,
triggers, or checks. The current oracle re-runs the original visible protected
suite; it does not yet add a separate hidden test suite.

Each oracle run is correlated using a fresh run ID, the pinned workflow revision,
and the candidate in its run title. The original verification artifact ZIP,
JUnit XML, environment record, and logs are archived with checksums.

An operator integration check has exercised this path successfully without an
LLM. It is stored separately as an operator check and is not an agent result.

## Setup

Install from the agent repository:

```sh
python -m pip install --editable ".[dev]" --constraint ci/constraints.txt
```

Create a local `.env` using `.env.example` and configure `OPENAI_API_KEY`. The CLI
loads that file; existing environment variables take precedence. GitHub access
can come from `GH_TOKEN`, `GITHUB_TOKEN`, `gh auth login`, or an explicitly
selected `BUILDTRACE_GITHUB_ACCOUNT` in Git Credential Manager.

The checked-in configurations use the current workspace's capture IDs. On a new
checkout, rebuild local archives from the exact declared run attempts:

```sh
python -m buildtrace experiment capture-inputs --spec experiments/training.json --output data/specs/training.json
python -m buildtrace experiment capture-inputs --spec experiments/evaluation.json --output data/specs/evaluation.json
```

These commands collect evidence without model calls or GitHub writes. Keep the
archives as part of the experiment's retained data; GitHub logs can expire.

Check local prerequisites:

```sh
python -m buildtrace experiment validate --spec data/specs/training.json
```

Validation reports missing inputs without making model calls. Live execution
also checks the trusted workflow, pins its revision and input-manifest hashes,
and saves a resolved `experiment.json` in the output directory.

## 1. Collect training sessions without memory

```sh
python -m buildtrace experiment run --spec data/specs/training.json --split train --output data/experiments/training-v1 --allow-publish
```

`--allow-publish` explicitly authorizes trial commits/branches, Actions runs, and
paid model calls. The pilot caps each trial at 20 model calls, 3 patch attempts,
8,192 output tokens per call, an 80,000-token cumulative stopping threshold,
and 1,200 seconds. Reasoning tokens are included in the output accounting.

Budgets are checked at operation boundaries. One in-flight model or network
operation can finish beyond a threshold; actual usage and elapsed time are
recorded. A truncated model response is an unsuccessful budget-limited attempt,
not a successful fix.

Each session preserves observable model messages, tool actions, results, brief
plans, patch hunks, and verification records. Private reasoning text is not
available; opaque Responses reasoning state is used only for within-trial API
continuation and is not fed into historical memory.

The runner stops on an infrastructure failure by default and retains its record.
`--continue-on-infra` explicitly continues with that failure still included.
Completed outcomes are reused on resume, including unsuccessful ones. An
incomplete trial directory is never silently overwritten.

## 2. Build and freeze WikiTrace memory

```sh
python -m buildtrace experiment build-memory --spec data/experiments/training-v1/experiment.json --records data/experiments/training-v1/results.jsonl --output data/memory/invoice-v1
```

This calls upstream `compile_trajectory` once per training session, with the
actual task, observable trajectory, and outcome. It supplies no reference fix or
expert explanation. The adapter enforces at most three insights per session and
preserves failed sessions as fallible interpretations.

The corpus contains official WikiTrace insight and compilation documents linked
to raw debugging sessions. Original initial and verification CI evidence is
copied byte-for-byte into each source run. A manifest fingerprints the corpus,
injected insight text, compiler settings, and upstream revision.

For this pilot, the 12 training sessions form one fixed corpus used by all test
repetitions. Results are conditional on that corpus. To study memory-construction
variation, repeat the complete training/build/evaluation process under new
experiment IDs.

Attach its verified hashes to the held-out evaluation configuration:

```sh
python -m buildtrace experiment attach-memory --spec data/specs/evaluation.json --corpus data/memory/invoice-v1 --output data/specs/evaluation-ready.json
```

The adapter rejects test task IDs, identical failing revisions, and held-out
scenario families found in historical memory. Compiler and evaluation model
settings must match. Test sessions never update the corpus during a comparison.

## 3. Run the same held-out tasks through all three modes

```sh
python -m buildtrace experiment run --spec data/specs/evaluation-ready.json --split test --output data/experiments/evaluation-v1 --include-reruns --allow-publish
```

Task order is seeded. Mode order rotates across repetitions so each mode occupies
each position once with three trials. Every trial has a fresh conversation,
candidate state, branch namespace, and CI verification. Tools cannot inspect
other trials' branches or historical GitHub runs outside their assigned access.

The shared loop checks the model ID actually returned by OpenAI. Reports reject
mixed model versions, budgets, snapshots, protocols, corpora, or insight hashes.
Incomplete comparison grids are also rejected.

## 4. Independently review unnecessary changes and evidence impact

Export an annotation form:

```sh
python -m buildtrace experiment review-template --records data/experiments/evaluation-v1/results.jsonl --output data/review.json
```

Hunk review forms show task IDs, code paths, and actual diffs; agent mode is
omitted to support blinded review. Mark a hunk `necessary`, `unnecessary`, or
`uncertain`, and supply an independent reviewer identifier. Valid alternative
repairs are not unnecessary merely because they differ from a reference patch.

Evidence-impact entries contain before/after structured plans and exact source
citations. Review whether the evidence supports the claimed plan change and
whether the subsequent actions adopt it. Labels are `confirmed-change`,
`confirmed-no-change`, or `uncertain`.

Apply completed annotations to a new derived results file:

```sh
python -m buildtrace experiment review --records data/experiments/evaluation-v1/results.jsonl --annotations data/review.json --output data/experiments/evaluation-v1/reviewed.jsonl
```

The form is bound to the original results file's SHA-256. Raw trial files and
trajectories remain intact. Unreviewed entries can remain in the form; they are
left unscored. Annotated test results are not used to build historical memory.

## 5. Generate the six requested measurements

```sh
python -m buildtrace experiment report --spec data/experiments/evaluation-v1/experiment.json --records data/experiments/evaluation-v1/reviewed.jsonl --output data/analysis/evaluation-v1
```

Use the original `results.jsonl` for an initial report before review. This writes
`summary.json` and `summary.md`.

### Successful fixes

Count trials whose final candidate passes both the target CI and trusted oracle.
Report the count, denominator, percentage, and number of tasks solved by majority.
The report never accepts the agent's assertion of success as verification.

### Regression

For each held-out task, the majority outcome over three no-memory reference runs
defines whether the baseline solves it. Regression is:

```text
Failed memory-enabled trials on baseline-solved tasks
----------------------------------------------------
All memory-enabled trials on baseline-solved tasks
```

This is task-level memory regression, distinct from breaking a previously passing
unit test inside a repair attempt.

### Gain

```text
Successful memory-enabled trials on baseline-unsolved tasks
----------------------------------------------------------
All memory-enabled trials on baseline-unsolved tasks
```

No eligible baseline failures means `N/A`, not zero Gain. Invalid no-memory
reference sessions make the affected task unscorable for Gain/Regression.

Optional independent no-memory reruns estimate ordinary model/execution variation.
The reference runs that define majority labels are not treated as an independent
noise floor.

### Unnecessary code changes

Measure independently reviewed unnecessary hunks and changed lines in the final
candidate diff, alongside total patch size and review coverage. Patch size alone
does not establish necessity. Incomplete or uncertain review produces `N/A` for
the full unnecessary-change count, plus the known reviewed count.

Earlier candidate patches remain in the trajectory; patch-attempt counts also
describe investigation churn.

### Resolution time

Use monotonic wall time and report successful-fix medians separately from all
attempt durations. CI verification-operation time and human approval waiting are
recorded separately. CI time includes remote setup, polling, and evidence
collection overhead; it is not a pure GitHub queue-time measurement.

The pre-authorized benchmark has zero interactive approval waiting. Failed and
timed-out attempts remain visible rather than being presented as fast fixes.

### Historical evidence changes the decision

WikiTrace records a plan before a contiguous source-reading burst and another
immediately afterwards. Changes compare actionable `action`, `target`, and
`parameters`, ignoring summary wording alone. Records carry source document
hashes and ranges.

Report lookups, complete before/after records, changed decisions, and the
independently reviewed effect separately. A current-environment observation
interposed before the post-evidence plan leaves that comparison incomplete.
These are observed decision changes associated with evidence access, not a claim
that the trace alone proves causality.

## Interpretation

Reports show counts and eligible denominators. Paired bootstrap intervals resample
tasks while keeping repeated trials together. They report WikiTrace's differences
from insights-only for success, Gain, and Regression, and from no-memory for
overall success. Lower Regression and higher Gain/success are favorable.

Compare successful repairs and regression together, then inspect time, change
overhead, and the source-linked cases explaining differences. The outcome may
support, fail to support, or contradict the hypothesis that WikiTrace helps on
this benchmark. The implementation does not manufacture a winning result.
