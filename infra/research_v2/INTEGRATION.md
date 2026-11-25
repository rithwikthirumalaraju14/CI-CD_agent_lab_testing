# Research-v2 infrastructure contract

For the completed `buildtrace.research-benchmark.v2` generator, use
`HOSTED_DEPLOYMENT.md` and the `hosted-*` files. The original template described
below is retained as the separate v1 standalone-package contract.

These are optional, unpublished operator templates. The existing invoice lab and
its captures retain their original workflow and artifact contract.

## Standalone package setup

1. Use a standalone package with `src/bench_app/`, `tests/`, `ci/constraints.txt`,
   editable `.[dev]` dependencies (pytest and build), and a CLI accepting
   `python -m bench_app examples/input.json`.
2. In a **new** benchmark repository/revision, copy `trusted-ci.yml` to the task's
   configured `.github/workflows/<workflow>`. Keep `verify.py` and
   `pytest-trusted.ini` at `infra/research_v2/` in that repository.
3. Copy `contract.example.json` to `infra/research_v2/contract.json`. Replace the
   null count with the exact positive number of original pytest test cases,
   including parametrized cases. The example deliberately cannot pass unchanged.
4. Keep the verifier, contract, original tests, and example input protected from
   candidate edits. Pin the complete trusted revision in `oracle_sha` and keep
   `oracle_ref` at it for the whole experiment. Call `pin_oracle()` on each backend;
   it also discovers the optional `dispatch_id` workflow input.
5. Configure the task's existing `workflow`, `required_steps`, and
   `expected_test_count`. For this template, the job is `bench-checks`; required
   steps should include `Check dependency consistency`, `Run tests`,
   `Build package`, `Run example`, and `Verify protected evidence`.
6. Select `artifact_prefix="bench-ci"` in the backend constructor, or declare
   `TaskSpec.artifact_prefix` with the legacy default `invoice-ci`. The constructor
   option takes precedence. The template's artifact name is
   `bench-ci-${{ github.run_id }}-${{ github.run_attempt }}`; change both sides if
   selecting a different prefix.

For separately held oracle tests, set the trusted contract's `tests_path` to a
directory such as `oracle_tests` on the pinned oracle revision. Do not include
that directory in the task's failing `base_sha` tree. The backend lists/reads only
the base snapshot plus allowed edits; the workflow checks out the oracle and
candidate into separate sibling directories. The candidate's normal target run
still uses its own `tests/`; its contract should name that public suite and its
count. Only the oracle revision's contract names the held suite and the task's
expected oracle count. The oracle executes the separately held original
suite with trusted discovery settings, excluding candidate conftests and plugin
autoload. No test source files are uploaded by this template. CI logs can still
include test names, failures, and tracebacks; decide which diagnostics the solver
receives before freezing the protocol.

## Dispatch and artifact contract

The dispatch inputs are `source_sha`, boolean `oracle_mode`, and optional string
`dispatch_id`. Workflows declaring `dispatch_id` must use this exact display title:

```text
BuildTrace target <candidate SHA> <dispatch ID>
BuildTrace oracle <candidate SHA> <dispatch ID>
```

The backend generates the ID once per dispatch intent. It snapshots existing run
IDs before the single POST, then matches workflow, event, branch, head SHA, and
title. A lost POST acknowledgement is reconciled using GETs; the POST is never
automatically repeated. Multiple matching runs are an infrastructure error.
Repeated calls on the same backend resume/reuse the same dispatch intent.

Artifacts must be uniquely named `<prefix>-<run ID>-<attempt>` and unexpired. The
entire original ZIP is checksum-captured before any member is inspected. It must
contain unique bounded files, at any archive depth:

- `pytest.xml`: exactly the configured number of test cases, with zero suite or
  individual-case failures, errors, or skips.
- `environment-before-install.json`: `checked_out_sha` equal to the candidate.
- `verification-proof.json` for every prefix other than `invoice-ci`:
  `schema_version: 1`, `oracle_mode: true`, candidate `checked_out_sha`, pinned
  `workflow_sha`, exact integer `expected_test_count` and `observed_test_count`,
  `protected_tests_unchanged: true`, `verification_complete: true`, a SHA-256
  `protected_tests_sha256`, and the exact integer `run_id` and `run_attempt`.

The trusted helper records the suite/contract digest before installing candidate
code, runs the original suite, and checks counts, checkout SHAs, and protected
files again after the build/example steps. It emits a failed proof when protected
verification fails. Green target CI alone never establishes a repair. Invoice
replay still accepts the original JUnit plus checkout-SHA contract.

## Required shared runner/model integration

`BackendError` now carries per-operation `elapsed_seconds`, JSON-safe
`diagnostics`, optional **nonpassing** `verification`, candidate `hunks`, and an
`observation` containing retained log catalogs. `as_dict()` serializes the core
failure record. `backend.last_failure` retains the last failure object;
`backend.ci_operation_seconds` accumulates successful and failed CI operations.
Exhausted oracle artifact-list/download GETs are infrastructure errors; completed
test/check failures remain nonpassing verifications.

The shared runner must, on `BackendError`:

- Preserve the `infra-error` outcome and journal `exc.observation`/`exc.as_dict()`.
- Add `exc.elapsed_seconds` to CI time **once**. Successful patch results already
  contribute `verification.elapsed_seconds`; do not also add the cumulative
  backend timer.
- Retain `exc.verification` when present and `exc.hunks`. A candidate or run that
  was never created has no fabricated URL, SHA, capture, or verification record.
  Keep candidate-attempt accounting distinct from completed verification count.

Keep `ValueError`/bad tool arguments recoverable as tool errors. In particular,
directory reads are neither infrastructure failures nor failed patch results.
The backend records elapsed infrastructure failures during submission; the
current shared runner must consume the new metadata to include it in its record.

When adding a declared `artifact_prefix` schema field, preserve the legacy
fingerprint/serialization path for old manifests and reports. Freeze the prefix,
trusted contract/workflow/helper hashes, retry policy, cancellation setting, and
runner integration as protocol inputs before evaluation. The existing runner's
fingerprint covers experiment Python files but not these external templates or
`github.py`/`evidence.py`; the coordinator must include their hashes explicitly.

## Bounds and limitations

- Staging-directory publish retries only `PermissionError`, with four waits
  totaling 0.75 seconds. It publishes the same staging bytes/ID with an atomic
  rename and never replaces an existing capture.
- GET defaults are three attempts, exponential 0.25/0.5-second waits, a 2-second
  exponential-delay cap, and a 5-second total wait cap. Only transport timeouts,
  network/remote-protocol errors, HTTP 429, and HTTP 5xx are retried. Long
  `Retry-After` values stop retries rather than being ignored. HTTP requests and
  archive reads honor the current operation deadline; retry limits are common
  across solver modes. Mutation POSTs and unsuccessful fixes are not retried.
- At an overdue dispatch deadline, cancellation is opt-out via
  `cancel_overdue_runs=False`. Only one known matching run is eligible: it must
  have this backend's unique dispatch title or a trial branch this backend
  created. A fresh GET must confirm head, branch, event, title, and original run
  attempt. Cleanup has a separate five-second budget and sends at most one
  cancellation POST. A cancellation request is not a claim that the run stopped.
- Unknown/not-yet-visible runs, ambiguous matches, unrelated runs, changed run
  attempts, and legacy shared-oracle runs without unique IDs are not cancelled.
  GitHub's workflow job timeout remains the backstop. Dispatch intent is held in
  memory: after process loss, an operator must reconcile recorded run IDs/titles;
  creating a fresh backend is not durable exactly-once dispatch.
- These templates isolate trusted verification inputs from agent snapshots and
  candidate discovery configuration. They do not sandbox malicious build/test
  code running on the same CI machine. Action versions, dependency installation,
  artifact retention, and live GitHub behavior still require coordinator review
  and a separate authorized development validation before an experiment freeze.
