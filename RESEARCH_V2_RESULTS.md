# Research-v2: development results and failed source-use gate

**The larger development experiment does not establish a WikiTrace advantage.
Both development candidates failed the predeclared original-source-use gate, so
the 24-case held-out comparison was not frozen or executed.**

These are genuine GPT-6 Luna repair sessions with real GitHub Actions execution
on **authored synthetic project failures**, not production-incident results or
fabricated model histories. The original [invoice pilot](PILOT_RESULTS.md) and its
corpus remain separate and unchanged.

## What was implemented and executed

- A 42-case, 39-specific-family catalogue across ingestion/configuration,
  versioned service/client integration, and artifact/report processing:
  **12 train / 6 development / 24 held-out** cases. Eighteen train/dev inputs were
  deployed and their exact initial failures archived.
- **36 genuine no-memory training sessions**, all ultimately repaired. Their
  37 candidate verifications retain one unsuccessful intermediate candidate.
- The actual commit-pinned upstream WikiTrace compiler produced **75 insights**
  from those 36 complete sessions. No operator reference smoke is a memory source.
- **Two complete development grids**, each six tasks x three modes x three
  repetitions = **54 trials**. The first grid also retains an unsuccessful
  intermediate candidate; all 54 final repairs passed. The second grid likewise
  completed all 54 final repairs.
- Identical strategic system guidance across modes, identical selected insight
  bytes across the two memory modes, source access confined to selected linked
  episodes, fresh trial state, and a single aggregate writer.
- Independent target CI plus a pinned trusted oracle requiring protected public
  tests, direct-wheel and sdist-rebuilt-wheel oracle tests, exact separate test
  counts, zero skips, import origins, candidate/workflow identity and distribution
  byte hashes. Four separate operator smokes validated the three domains and both
  dependency/API generations before training.

This is specific-failure-family separation, not project/source-code separation.
The projects share code; a genuinely observed historical source file may contain
a healthy implementation of a mechanism broken in a later snapshot.

## Six measurements: revised development candidate

**Development diagnostics, available for tuning; not held-out scores.** Each mode
has 18 trials on six tasks. Common settings: `gpt-6-luna`, medium reasoning,
temperature omitted, 60 calls, five submissions, 16,384 output tokens/call,
4,000,000 cumulative input+output tokens including cached input, 3,600 seconds,
120-second model-request ceiling, and fixed three-cell execution blocks.

| Measurement | No memory | Insights only | WikiTrace |
| --- | --- | --- | --- |
| Verified final repairs | 18/18 | 18/18 | 18/18 |
| Regression on baseline-solved tasks | Reference | 0/18 | 0/18 |
| Gain on baseline-unsolved tasks | Reference | N/A | N/A |
| Independently reviewed unnecessary hunks | 4 | 4 | 4 |
| Median successful wall time | 123.33 s | 122.75 s | 123.41 s |
| Median active time, subtracting CI wait | 29.58 s | 30.05 s | 27.96 s |
| Historical requests / raw deliveries | 0 / 0 | 0 / 0 | 1 / 0 |
| Reviewed adopted material effects | N/A | N/A | 0/1 reviewed |

Gain is N/A because every development task was baseline-solved. All 66 final
hunks were reviewed: 54 necessary and 12 unnecessary. There were no recorded
live-trial infrastructure outcomes. Small timing differences, dominated by CI
waiting, do not establish a speed advantage.

The single WikiTrace lookup occurred on `rv2-034`. It read a **model-written
compilation**, not an original raw observation. A structured plan changed from
`investigate` to `adapt`, producing one syntactic before/after change. The
independent evidence reviewer found **no material change and no adopted
source-induced refinement**: the patch followed the already identified sdist
repair direction. The compilation did not establish the requested original
sdist failure sequence or include rule. Syntactic changes remain in the JSON;
the v2 report headline shows reviewed adopted effects instead.

## Preserved first candidate and retrieval revision

| First-candidate measurement | No memory | Insights only | WikiTrace |
| --- | --- | --- | --- |
| Verified final repairs | 18/18 | 18/18 | 18/18 |
| Unnecessary final hunks | 2 | 0 | 1 |
| Median successful wall time | 121.30 s | 127.02 s | 121.62 s |
| Historical requests / raw deliveries | 0 / 0 | 0 / 0 | 0 / 0 |

The first selector queried all initial logs, including successful checkout and
tool-provisioning output. Four different tasks received identical packets of
mostly generic advice. The revised selector uses the earliest failed step per
failed job, its original step logs, diagnostic fallback windows, wrapper removal,
and fixed generic token filtering. It uses no operator mechanism labels, reference
repairs, later solver actions, or outcomes. Both memory modes received the same
revised packets; prompts, corpus, budgets and the gate remained fixed.

This was a new development candidate, not a replacement of unfavorable results.
Neither grid is pooled with the other or described as an independent held-out
evaluation. The revised retrieval produced different task packets but did not
demonstrate original-source inspection or material evidence benefit.

## Gate and decision

Before development, `rv2-019` and `rv2-033` were selected from distinct specific
families. Each required at least two of three WikiTrace repetitions with an exact
linked **raw original observation**, fresh before/after decisions, independent
materiality review and observable adoption; at least one complete chain had to
end in verified repair. No score superiority or reading on every task was required.

**Both gates failed.** Neither candidate delivered any raw historical observation.
No protocol/evaluation freeze or held-out trial was produced. The 24 held-out
cases remain unmaterialized and unexecuted in this round.

The current development set is too easy for distinguishing repair success, and
the model generally resolves these self-contained failures from current facts.
The next useful research step is genuinely harder, multi-stage/multi-module
development failures with naturally relevant historical uncertainty. Relaxing
the gate or forcing reads would not supply the missing evidence.

## Review, integrity and checks

Hunks were independently reviewed by fresh-context coding-agent sessions using
anonymous IDs and original public requirements/source. Hunk reviews were locked
before exposing the separate evidence package. These are **agent reviews, not
a human panel**. The first pass left nine packaging hunks uncertain because
custom backend context was missing; after supplying the original public backend,
a second review resolved them. Both passes and rationale are retained.

An offline audit replayed **146 target/oracle verification pairs**, including
both unsuccessful intermediate candidates, checksum-verified **310 distinct
model-related initial/verification captures**, and verified the actual injected
packet text and shared system guidance from trajectories. It did not rerun all
candidate programs locally or selectively rerun unsuccessful trials.

- Final combined local suite: **399 passed**.
- Dependency consistency: `python -m pip check` passed.
- Legacy pilot experiment fingerprint and report replay were preserved.
- Windows preparation locks discovered during setup/checks received bounded,
  byte-preserving publication retries. Setup/check failures are retained as
  operational notes, distinct from the zero live-trial infrastructure outcomes.

Recorded usage across training and both development candidates: 1,667 repair-model
calls, 13,324,327 input tokens (11,731,336 cached), and 249,613 output tokens.
The 36 compiler calls used 841,788 input and 21,450 output tokens. These counts
are not a billing invoice; zero upstream transport cost metadata does not mean
the compiler calls were free.

## Retained artifacts

Local Git-ignored artifacts under `data/research-v2/`:

- `DEVELOPMENT_PLAN.md`, `DEVELOPMENT_REVISION_2.md`: predeclared controls/revision.
- `experiments/{train-v1,dev-v1,dev-v2}/`: frozen specs, phase manifests, original
  results, trajectories and derived reviewed results.
- `memory/train-v1/`: immutable official 75-insight corpus and admission ledger.
- `analysis/dev-v{1,2}/final-report/`, `gate-reviewed.json`, `review/`: all six
  metrics, failed gates, separate review forms/annotations and rationale.
- `analysis/dev-v{1,2}/runtime-source/`: exact checksum-matched execution/control
  source archived before later implementation/presentation fixes.
- `analysis/development-audit.json`: proof replay, archive/packet integrity,
  token ledger, source hashes and cohort identities.

Corpus SHA-256:
`83f9025d005fc7e0382935b4dfce44ffa659452851e9ba7b15162dc892a42bb7`.
Full-insights SHA-256:
`168ee7efb391317d6bf6c34bfa4132a9aea920b37ae0dc39f332bf5fe3a29e04`.

Implementation and reproduction contracts:
[research controls](docs/RESEARCH_PROTOCOL.md),
[benchmark coverage](benchmarks/research_v2/COVERAGE.md),
[hosted deployment](infra/research_v2/HOSTED_DEPLOYMENT.md), and
[foreground operations](benchmarks/research_v2/OPERATIONS.md).
