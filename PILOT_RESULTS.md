# BuildTrace live pilot: invoice-evaluation-v2

## Conclusion

**This pilot does not establish that WikiTrace is better than both baselines.**

The WikiTrace-enabled mode and no-memory mode each verified 11 of 12 primary
repairs. Insights-only verified 7 of 12. However, the agent never requested or
opened historical compilation/raw evidence in the scored evaluation. The
observed difference from insights-only therefore cannot be attributed to
checking original evidence.

The comparison exercised the complete live pipeline, but the four small held-out
cases did not demonstrate the project's central evidence-reassessment mechanism.

## Setup and retained data

- Model for every solver mode and compiler: `gpt-6-luna`, medium reasoning,
  Responses API; no temperature parameter.
- Four training failure types and four disjoint held-out scenario families in
  one invoice-calculator project.
- Twelve training sessions attempted; eleven have complete records. One
  background-process interruption is preserved and excluded from memory.
- The official pinned WikiTrace compiler produced **24 insights from 11 complete
  sessions**. Both memory-enabled modes received identical frozen insight text.
- Scored evaluation: **36 primary trials** (4 tasks x 3 modes x 3 repetitions).
- Additional noise reference: **12 independent no-memory reruns**.
- All **48 scored-cohort records** are retained, including infrastructure errors.
- Same per-trial limits: 20 model calls, 3 patches, 8,192 output tokens per call,
  80,000 cumulative token stopping threshold, and 1,200-second wall deadline.

## The six requested measurements

| Measurement | No memory | Insights only | WikiTrace-enabled |
| --- | ---: | ---: | ---: |
| Verified successful fixes | 11/12 (91.7%) | 7/12 (58.3%) | 11/12 (91.7%) |
| Regression on eligible baseline-solved tasks | Reference | 5/9 (55.6%) | 0/9 (0%) |
| Gain on baseline-unsolved tasks | Reference | N/A | N/A |
| Operator-reviewed unnecessary final-candidate hunks | 0 | 0 | 0 |
| Median wall time for verified successful repairs | 115.99 s | 124.91 s | 110.66 s |
| Historical evidence changes the decision | N/A | N/A | N/A: 0 source reads |

### Successful fixes and failures

Success requires the candidate CI and the independently dispatched trusted oracle
to pass, including all 36 original tests and proof of the checked-out candidate.

| Outcome | No memory | Insights only | WikiTrace-enabled |
| --- | ---: | ---: | ---: |
| Verified solved | 11 | 7 | 11 |
| Agent unsuccessful | 0 | 3 | 1 |
| Infrastructure error | 1 | 2 | 0 |

All four agent-unsuccessful outcomes stopped at the token budget. They are kept
as unsuccessful agent outcomes, not reclassified as infrastructure problems.

The additional no-memory reruns had 8/12 verified fixes, one token-budget failure,
and three infrastructure errors. They are a noise reference, not pooled into the
primary no-memory score.

### Regression and Gain denominators

The baseline labels use no-memory reference runs, not the historical training
sessions. Three held-out tasks have valid baseline-solved labels. Case-008 is
unscorable for Gain/Regression under the predeclared strict rule because one
no-memory reference run had an infrastructure error.

That leaves 3 tasks x 3 repetitions = **9 eligible trials per memory method** for
Regression. The full 5/9 insights-only regression count includes two
infrastructure outcomes; it is not five demonstrated memory-caused wrong fixes.

There are **zero eligible baseline-unsolved tasks**, so Gain is **N/A**, not zero
and not evidence that memory can solve a previously unsolved task.

### Infrastructure sensitivity

The sensitivity rule was recorded before the completed comparative report:
exclude the same task/trial from all three primary modes if any mode has an
infrastructure outcome. Keep agent budget failures. Retain the original baseline
labels rather than redefining them after exclusions.

Three matched cells are excluded: case-006/trial-2, case-007/trial-2, and
case-008/trial-2. Nine matched cells remain per method:

| Method | Verified fixes in paired infrastructure sensitivity |
| --- | ---: |
| No memory | 9/9 (100%) |
| Insights only | 6/9 (66.7%) |
| WikiTrace-enabled | 9/9 (100%) |

Eligible sensitivity Regression is 3/7 for insights-only and 0/7 for
WikiTrace-enabled. Gain remains N/A. This supplementary view still does not show
WikiTrace outperforming no memory.

### Unnecessary code changes

All 44 recorded final-candidate hunks, across primary trials and reruns, were
reviewed using the recorded mode-blinded operator rubric. All were judged causal
parts of valid repairs, giving zero unnecessary hunks under that rubric.

This includes legitimate alternatives such as moving the colliding local helper
to a non-colliding module and relocating the currency setting to the application
step. A larger valid alternative is not automatically unnecessary.

Primary changed-hunk/changed-line totals were 11/31 for no memory, 10/20 for
insights-only, and 14/40 for WikiTrace-enabled. Patch size and semantic necessity
are distinct measurements. Infrastructure-interrupted proposals without a
completed hunk record were not automatically graded as complete patches.

This is one operator/model review independent of the Luna repair conversations,
with explicit method labels omitted from forms. It is not a multi-reviewer human
adjudication; alternative semantic judgments remain a study limitation.

### Resolution time

Median active successful-repair time, after subtracting recorded CI-operation
time, was 31.44 s for no memory, 30.09 s for insights-only, and 25.67 s for
WikiTrace-enabled.

CI queueing varied substantially and some jobs exceeded the wall deadline.
Recorded CI-operation time includes polling and archival overhead, not just
queue time. Failed verification operations may lack a completed CI-time
breakdown. These small successful-repair median differences do not establish a
general speed advantage.

### Historical evidence impact

An independent parse of the original trajectories found **zero requested,
executed, delivered, or errored `read_historical_evidence` calls** in the primary
evaluation. There are no source-reading before/after events to review.

The evidence-change rate has denominator zero and is **N/A**. Do not report it
as a measured 0% causal effect. The official compiler and source links were
present, but the solver's ability to inspect them was not exercised.

## Confidence and interpretation

There are only four held-out tasks. The paired task-bootstrap 95% interval for
WikiTrace-enabled minus insights-only successful-fix rate is approximately
**-16.7 to +83.3 percentage points**; it includes zero. The observed successful-fix
rate difference from no memory is zero.

The three insights-only agent failures and the one WikiTrace-enabled agent
failure were token-budget stops. Repeated memory injection increases prompt
size. Prompt guidance, resource consumption, and sampling variation can affect
these outcomes even when no source evidence is opened.

Therefore, this pilot supports an operational statement—an evidence-linked
memory pipeline and controlled comparison are runnable—but **does not demonstrate
that original-evidence verification caused better repairs**.

## Verification audit and costs

- Replayed all **37 recorded passing target/oracle pairs**, including reruns.
- Verified checksums for **74 associated captures**.
- Rechecked original test counts and candidate-SHA environment proof.
- Reverified the frozen corpus and injected-insight hashes.
- Scored evaluation: 3,262,064 input tokens (2,897,402 cached) and 46,064 output
  tokens, including reasoning.
- Estimated standard text-token charge for evaluation-v2: **about $0.09**.
- Known recorded model responses across training, compilation, the superseded
  partial run, and evaluation-v2: 876; estimated text-token charge **about
  $0.16**, or about $0.18 if all uncached input were billed as cache writes.

These are token-based estimates at documented Luna rates, not billing invoices.
GitHub costs and other service-tier/regional/long-context fees are excluded.
Reasoning tokens are already included in output and are not counted twice.

## Operational limitations preserved

The detached training runner was interrupted before its last result was fully
archived. The incomplete source was not selectively replaced. A partial first
evaluation exposed a real directory-response bug in the shared file reader.
That partial cohort was preserved; the backend was corrected and all evaluation
modes restarted under the common corrected protocol.

The final cohort retained six infrastructure outcomes across primary runs and
reruns: Windows archival permission errors, CI deadlines, and a GitHub polling
failure. None was selectively rerun to improve a method's score.

## Next research step

Pre-register a larger, harder held-out benchmark across multiple projects, with
genuine scope mismatches, incomplete/conflicting historical lessons, and failed
debugging experiences. Use development tasks to ensure both memory modes have
enough context budget to complete diagnosis and that selective source access is
actually usable before freezing the test protocol. Keep the same insight
selection and compute rules for both memory-enabled modes.

Assess a new held-out cohort rather than tuning this one until a preferred method
wins. Preserve the current result as the first live pilot.

## Artifacts

- [Core metrics](data/analysis/evaluation-v2/report/summary.md) and
  [JSON](data/analysis/evaluation-v2/report/summary.json).
- [Sensitivity, per-task diagnostics, and verification audit](data/analysis/evaluation-v2/diagnostics.json).
- [All 48 run records and CI links](data/analysis/evaluation-v2/run-index.csv).
- [Independent annotation form](data/analysis/evaluation-v2/reviews.json) and
  [review rationale](data/analysis/evaluation-v2/review-decisions-notes.md).
- [Recorded-token cost ledger](data/analysis/evaluation-v2/token-cost-ledger.json).
- [Operational notes](data/pilot-operation-notes.md).
- [Frozen experiment](data/experiments/evaluation-v2/experiment.json).
- [Frozen memory manifest](data/memory/invoice-v1/corpus.json).

The `data/` directory is local and Git-ignored; retain or export it with the
report for a complete audit trail.
