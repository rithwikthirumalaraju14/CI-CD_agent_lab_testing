# BuildTrace Implementation Plan

## Purpose

**Primary objective:** compare the no-memory, insights-only, and WikiTrace agents
under the same model, tasks, and repair budget. Measure successful fixes, memory
Regression, Gain, independently reviewed unnecessary changes, resolution time,
and how historical evidence affects decisions.

The current pilot uses `gpt-6-luna` with medium reasoning through OpenAI's
Responses API. The exact experiment protocol and runnable commands are maintained
in [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md); upstream reuse is documented in
[THIRD_PARTY.md](THIRD_PARTY.md). Establishing whether WikiTrace helps is the
experiment's hypothesis, and conclusions must follow measured outcomes.

BuildTrace is an AI-powered CI/build failure debugging assistant that learns from previous debugging sessions while checking whether historical solutions apply to the current failure.

The core contribution is evidence-backed experience reuse: the agent can follow a short lesson back to the original debugging session, inspect what actually happened, and apply, adapt, or reject the lesson.

This plan is based on the full 40-page paper, [Don't Just Trust Your Memory: Evidence-Linked Memory Reduces Memory-Induced Degradation in LLM Agents](sources/2611.14320v1.pdf), including its memory formats, prompts, and evaluation method.

The recommended first implementation is a small webpage connected to GitHub Actions. GitHub Actions runs the project; BuildTrace handles investigation, decision-making, and memory.

## 1. What the First Version Should Do

The user experience:

```text
Paste a failed GitHub Actions run link
                  |
                  v
BuildTrace reads the logs and relevant project files
                  |
                  v
Finds lessons from previous debugging sessions
                  |
                  v
Checks historical evidence when the lesson is questionable
                  |
                  v
Shows the diagnosis, supporting evidence, and proposed changes
                  |
                  v
You approve the patch
                  |
                  v
BuildTrace creates a repair branch and starts CI
                  |
                  v
Shows the result and records the debugging session
```

Start with repositories you control that use Python, pip, pytest, and GitHub Actions, covering four failure categories:

| Category | Initial examples |
| --- | --- |
| Dependency conflict | Two packages require incompatible versions |
| Import error | Missing dependency, changed import, local file shadowing a package |
| Test failure | Incorrect application behavior or runtime error |
| Configuration issue | Missing environment variable or incorrect test configuration |

## 2. Principles to Preserve from WikiTrace

### A. Both Insights and Compilations Can Be Wrong

A compilation is an AI interpretation of a session. A successful outcome also does not prove that every action contributed to success.

Every important claim should therefore lead back to the actual commands, outputs, changes, and test results.

### B. Evidence Inspection Should Be Selective

BuildTrace should start with short lessons, then inspect their sources when a specific question needs answering.

This keeps historical evidence useful without loading entire debugging histories into every decision.

### C. Historical Records Should Stay Intact

When a lesson is challenged, record the new finding separately and link it to the original. This preserves the ability to explain what the agent knew and why its decision changed.

### D. Evaluation Needs Frozen Memory

During a comparison experiment, all methods should use the same fixed historical corpus. New evaluation sessions become eligible for future memory only after the experiment ends.

These principles come primarily from Sections 3-5 and Appendices B and F of the paper.

## 3. Implementation Workflow

### Step 1: Connect to a Failed GitHub Actions Run

Build a GitHub connection that collects:

- Repository and tested revision.
- Workflow file and relevant job configuration.
- Run ID, attempt number, failed job, and failed step.
- Complete available logs.
- Relevant source files, dependency declarations, and tests.

Archive the collected evidence locally with stable identifiers. Historical evidence needs to remain available even after GitHub's original logs expire.

**Deliverable:** Paste a failed-run link and see the failure, relevant files, and source logs on one page.

### Step 2: Investigate on GitHub's Runners

BuildTrace will sometimes need more information than the original log contains.

For connected repositories, add a small, manually triggerable diagnostic workflow. BuildTrace can ask it to collect:

- Python and pip versions.
- Installed dependency versions.
- Dependency consistency checks.
- Which file an imported module comes from.
- Environment-variable presence.
- Results from a specific failing test.

Batch related checks into one run to reduce waiting.

The prototype application can run on your computer while these commands execute through GitHub Actions.

**Deliverable:** BuildTrace can answer questions about the actual CI environment using recorded command results.

### Step 3: Build the Basic Debugging Loop

Implement one debugging agent with tools for:

1. Reading current project files and logs.
2. Requesting CI diagnostics.
3. Forming a diagnosis.
4. Producing a patch.
5. Inspecting verification results.

Use explicit session stages:

```text
Collect -> Investigate -> Propose -> Await approval
                                         |
                                         v
                                 Apply -> Verify
                                            |
                                            v
                                      Finish or retry
```

Give each session a bounded investigation and repair budget, such as a maximum of three patch attempts initially.

**Deliverable:** The no-memory agent can take a real failing Python build through an approved repair and CI verification.

### Step 4: Make Verification Trustworthy

After approval:

- Create a repair branch based on the tested revision.
- Commit the approved patch.
- Start a new CI execution against the patched revision.
- Associate the result with that exact patch.

**Important GitHub behavior:** "Re-run jobs" uses the original commit and reference. Testing a fix therefore requires a new run against the changed code. See [Re-running workflows and jobs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).

A repair counts as successful when:

- The original failure is resolved.
- Required build and test checks run successfully.
- Previously passing behavior remains intact.
- The result is reproducible from the patched repository configuration.

The evaluator should also detect attempts to obtain a green result by disabling tests or skipping required checks.

## 4. Implement the Three Memory Layers

Every debugging session should produce the following records:

| Layer | Contents |
| --- | --- |
| Raw trajectory | Tool calls, CI commands, outputs, environment observations, patches, test results, and brief decision notes |
| Compilation | What failed, what was investigated, what was attempted, what happened, and the evidence supporting the interpretation |
| Insight | A short reusable lesson, its applicability conditions, source-session outcome, and links to the compilation and raw evidence |

### Example Insight

```text
Insight:
An import error may come from a local file shadowing an installed package.
Check the imported module's file location before changing dependencies.

Applicability:
Useful when the package appears installed but importing it behaves unexpectedly.

Sources:
Compilation: session-014
Raw evidence: diagnostic-03, output lines 12-18
```

### Memory Construction Rules

- Generate a compilation and up to three insights per session, following the paper.
- Include successful and unsuccessful debugging sessions.
- Mark unsuccessful-session explanations as hypotheses.
- Validate that cited evidence identifiers and quotations exist.
- Preserve contradictory lessons so the agent can investigate their differences.

For the small initial corpus, provide all insights to both memory-enabled modes, as the paper does. Highlight relevant ones using error type, packages, versions, Python version, and workflow stage.

Larger-scale retrieval can be evaluated once the evidence-checking mechanism works.

## 5. Build the Evidence-Checking Behavior

Before using a historical lesson, the agent compares its conditions with the current failure.

| Situation | Required investigation |
| --- | --- |
| Important details are missing | Open the compilation and relevant source events |
| Two lessons recommend conflicting fixes | Compare the evidence behind both |
| The lesson came from an unsuccessful session | Inspect what actually failed and what remains uncertain |
| Current versions or environment differ | Identify whether the historical procedure still applies |
| A memory-based fix fails | Revisit its evidence before another attempt |
| The agent remains uncertain | Look for a specific missing fact or gather current diagnostics |

The agent should record a short decision:

```text
Historical recommendation: Upgrade package Y.
Question: Did that fix work under conditions comparable to this project?
Evidence consulted: session-008, commands 4-7.
Current difference: This project requires an older framework version.
Decision: Reject the broad upgrade and investigate a compatible version.
```

Every decision should resolve to one of:

- **Apply** the lesson.
- **Adapt** it to the current conditions.
- **Reject** it.
- **Investigate further**.

This decision record is central to demonstrating that evidence affected the agent's behavior.

## 6. Keep the Implementation Simple

Organize the application into these components:

| Component | Responsibility |
| --- | --- |
| GitHub Actions connection | Fetch logs, request diagnostics, start verification, track results |
| Session coordinator | Track progress, approvals, retries, and budgets |
| Debugging agent | Investigate and propose patches |
| Memory compiler | Turn completed sessions into linked compilations and insights |
| Evidence store | Preserve original records and support targeted reads |
| Experiment runner | Execute and score the three comparison modes |
| Small webpage | Display progress, evidence, patch approval, and comparisons |

A practical initial implementation can use:

- One Python application and a background worker.
- A small database for session state and searchable metadata.
- Markdown and structured log files for the linked evidence corpus.
- A thin adapter for the selected LLM provider.
- A simple browser interface.

The CI connection should expose a small common interface, allowing CircleCI to be added later.

## 7. Implement A, B, and C as Modes of the Same Agent

This is essential for a fair comparison.

| Mode | Historical information available |
| --- | --- |
| A: No memory | Current repository, current failure, current diagnostics |
| B: Insights only | Everything A receives, plus historical insights |
| C: Evidence-linked memory | Everything B receives, plus source compilations and raw trajectories |

Keep the model, current-project tools, repair limits, and verification rules consistent.

B and C should receive the same insight text and applicability information. Tool permissions should enforce access differences, including access to historical GitHub logs.

Record the additional time and tokens spent reading evidence.

## 8. Build a Benchmark That Tests the Idea

Start with four cases, one per failure category, to check the complete workflow.

Then expand to a manageable evaluation set, such as:

- 12 historical training scenarios for collecting debugging sessions.
- A separate development set for tuning prompts and behavior.
- 24 held-out evaluation scenarios, six per failure category.
- Three runs per evaluation task and mode.

Keep reference fixes and evaluator-only tests outside the agent's accessible context.

Include situations where historical memory is:

- Helpful and applicable.
- Valid but incomplete.
- Valid for a different environment.
- Contradicted by another lesson.
- Derived from an unsuccessful attempt.

Use naturally generated memories for the main experiment. Clearly label deliberately misleading memories as separate stress tests.

### Measurements

| Metric | Meaning |
| --- | --- |
| Successful repair rate | Tasks actually fixed and verified |
| Gain | Success on tasks the no-memory baseline usually fails |
| Memory regression | Failure on tasks the no-memory baseline usually solves |
| Code regression | Previously passing behavior broken by a patch |
| Change overhead | Patch size, plus reviewed unrelated changes |
| Resolution time | Diagnosis and verification time, with CI queue and approval waits separated |
| Cost | Model usage and CI execution minutes |
| Evidence impact | How often consulting a source changes the recorded plan or patch |

Use the majority outcome of repeated no-memory runs as the reference for Gain and Regression. Additional no-memory reruns help estimate ordinary run-to-run variation.

## 9. Recommended Build Order

| Milestone | Completion condition |
| --- | --- |
| 1. Failure viewer | A real Actions link produces an archived, correctly identified failure report |
| 2. Basic repair | Agent A proposes a patch, receives approval, and verifies it through a new CI run |
| 3. Linked memory | Recorded sessions produce navigable insights, compilations, and raw evidence |
| 4. Evidence-aware repair | Agent C selectively opens sources and records apply/adapt/reject decisions |
| 5. Comparison demo | A, B, and C run on the same cases and produce an auditable results report |

## 10. What the Finished Demo Should Show

The strongest demonstration is a same-looking failure whose historical fix has different applicability conditions.

Show:

1. The retrieved lesson.
2. The source evidence the agent opened.
3. The important difference in the current project.
4. How that changed the proposed repair.
5. The resulting GitHub Actions verification.

The first implementation milestone should be one real failed Actions run progressing to an approved patch and a correctly verified result. That gives us the execution and evidence-recording foundation needed to test WikiTrace's contribution properly.

## References

- [WikiTrace source paper](sources/2611.14320v1.pdf).
- [GitHub Actions: Using workflow run logs](https://docs.github.com/en/actions/how-tos/monitor-workflows/using-workflow-run-logs).
- [GitHub Actions: Re-running workflows and jobs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).
- [GitHub Actions: Triggering a workflow](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
