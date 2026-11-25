# Hosted deployment: generator contract v2

Use `hosted-ci.yml`, `hosted_verify.py`, and `prepare_hosted.py` for the completed
`buildtrace.research-benchmark.v2` generator. The earlier `trusted-ci.yml` /
`verify.py` template remains the separate v1 contract; invoice replay is unchanged.

## Prepare new trees locally

First materialize **one original broken case** and its separate verifier bundle
using the generator API. Then run this stdlib-only operator utility:

```text
.venv/Scripts/python.exe infra/research_v2/prepare_hosted.py --project NEW_GENERATED_PROJECT --verifier-dir NEW_GENERATED_BUNDLE --public-destination NEW_PUBLIC_TREE --trusted-destination NEW_TRUSTED_TREE
```

All input/output trees must be disjoint, output parents must exist, and both output
leaves must be new. The utility checks the generator's protected/oracle hashes
before copying anything. It emits recommended task fields to stdout, outside both
deploy trees. It performs no Git, installation, HTTP, or publishing operations.
Atomic stage publication retries only transient permission locks, keeping the
same bytes and destination, with four waits totaling at most 0.75 seconds.

The public output contains the generator's standalone project, with exactly these
deployment additions/replacements:

```text
.github/workflows/ci.yml          <- hosted-ci.yml (replaces self-hosted workflow)
.github/research_v2/verify.py     <- standalone hosted_verify.py
.github/research_v2/public.json   <- public schema + original public test count
```

The public descriptor has only `schema_version` and `expected_public_tests`.
No catalogue, case ID, reference edits, generator manifest, original absolute
paths, hidden tests, or oracle answers are copied into this tree. The hosted
runtime is generic; it imports neither buildtrace nor WikiTrace.

The trusted output has exactly this deploy contract:

```text
.github/workflows/ci.yml
.github/research_v2/verify.py
.github/research_v2/public.json
infra/research_v2/contract.json
infra/research_v2/oracle/tests/test_oracle.py
infra/research_v2/oracle/fixtures/checks.json
```

The first three files match the public tree byte-for-byte. Only hash-declared
oracle files are copied from the generator bundle; its operator `manifest.json`
is omitted. The trusted tree does not need an installed application or agent.

Replacing the generator's protected workflow requires a **new deployment
contract**. The preparer updates its protected file map, initial snapshot hash,
and required step names, while preserving all original test/context/data/vendor
hashes and separate public/oracle counts. It writes canonical contract bytes:
UTF-8 JSON, sorted keys, compact separators, `ensure_ascii=False`, no NaN, **no
trailing newline**. `verification_contract_sha256` is SHA-256 of these exact bytes.
Do not regenerate the contract after a candidate repair; editable source changes
are checked against the same frozen deployed contract.

## GitHub layout and task fields

The simplest supported layout uses **one immutable oracle ref per case**:

- Publish each public input tree on an input branch such as
  `buildtrace-inputs/<opaque-name>` in the selected benchmark repository.
- Publish its matching trusted tree on `buildtrace-oracles/<opaque-name>`, pin its
  exact SHA, and keep that ref fixed. Different cases can share a repository and
  runtime bytes, but use distinct oracle refs/contracts.
- Also make the workflow available at the same path on the repository's default
  branch: GitHub requires that for `workflow_dispatch` availability.
- Push triggers apply only to `buildtrace-inputs/**`, providing initial failing
  CI evidence. Backend-created `buildtrace-trials/**` commits and oracle refs use
  explicit dispatches, avoiding extra push-triggered verification runs.

Use the preparer's fields plus repository/base SHA/failing capture/oracle ref/SHA:

```text
TaskSpec.schema_version = 2
artifact_prefix = "bench-ci"
workflow = "ci.yml"
expected_test_count = <original public count>
expected_oracle_test_count = <oracle count PER distribution, not a combined total>
verification_contract_sha256 = <canonical deployed contract SHA-256>
oracle_contract_path = "infra/research_v2/contract.json"
editable_environment_keys = []
editable_paths / protected_paths = <preparer output>
required_steps = {
  "checks": [
    "Resolve offline project", "Run protected tests", "Build distributions",
    "Run example", "Check installed distributions", "Verify protected evidence"
  ]
}
```

The backend accesses new fields with `getattr`. A missing oracle count falls back
to `expected_test_count` for legacy verification. For v2, `pin_oracle()` obtains
the separate count and digest from the trusted contract when omitted, returning
them on the pinned task. The coordinator must declare/persist these fields before
freezing a live experiment; legacy TaskSpec serialization/fingerprints need their
existing compatibility path. Keep schema version 1 as the legacy task default.

V2 pinning checks the configured contract, public/oracle counts, workflow bytes,
runtime, descriptor, and each separate oracle file at the pinned oracle SHA.
The oracle bundle is the `oracle/` sibling of the configured contract. The supplied
workflow uses the default path above; any path customization must also be frozen
in its `CONTRACT_PATH`. Source tools continue to list/read only the task's fixed
base SHA and allowed candidate edits, never the protected oracle revision.

## Hosted execution and evidence

Actions checkout, setup-python, and upload-artifact use full commit pins. Jobs run
on `ubuntu-24.04`, Python `3.11.11`, with a 15-minute job timeout. The workflow
provisions pinned pip/pytest/build and their pinned tooling dependencies from
PyPI into an external clean tool venv. All subsequent project pip commands use
`--no-index`, `--no-build-isolation`, protected `vendor/wheels`, and constraints.
Inherited package indexes/find-links are removed. URL/path requirements and
constraint index overrides are rejected before pip runs.

Project resolution uses pip **dry-run**, preserving the clean interpreter. The
selected bundled wirekit wheel is explicitly placed on each probe's import path;
it is selected independently of any host installation. Public tests use source.
The CLI and every test subprocess receive `BENCH_PROJECT_ROOT=<candidate>` so the
generator's public context fixtures retain their intended root.

The public pipeline produces pytest diagnostics, a wheel and sdist, the protected
`ci/check_distributions.py` direct/rebuilt release smoke checks, the example output
check, and per-phase logs. The independently dispatched oracle additionally
requires all eight phases:

```text
protected resolve public build cli wheel_oracle sdist_rebuild sdist_oracle
```

Both oracle suites run from external working directories against distinct actual
wheel paths, without source PYTHONPATH/config/conftest fallback. Each proves its
`bench_app.__file__` comes from its wheel and records that wheel's hash. Both must
run exactly the separate oracle count with zero failures/errors/skips; the public
suite must independently run exactly its public count. A source-only pass is not
a verified repair. Public release smoke failure does not suppress the independent
oracle distribution probes; any failed/missing phase still rejects the candidate.

Each dispatch uploads `bench-ci-<run_id>-<attempt>` containing:

- `environment-before-install.json`: actual candidate/workflow Git HEADs, counts,
  run identity, mode, and (oracle only) canonical contract digest.
- `pytest.xml`, and oracle-only `wheel-oracle.xml` / `sdist-oracle.xml`.
- `phase-results.json`, `verification-proof.json`, generic phase/origin logs.
- `distributions/direct/<wheel>`, `distributions/direct/<sdist>`, and oracle-only
  `distributions/rebuilt/<wheel>`.
- Oracle-only `verification-contract.json`, the exact canonical contract bytes.

Finalization checks the protected project/oracle tree and contract again, actual
checkout SHAs, exact report counts, all phase statuses, origin evidence, and
unchanged distribution bytes. Missing phases are explicitly nonpassing. Backend
verification independently checks the three JUnit reports, phase/count/origin
proofs, canonical contract hash, protected tree digest, and hashes of the uploaded
original distributions. Both target and oracle artifacts are immutable captures;
transport failures retain diagnostics and CI-operation timing through BackendError.

## Remaining integration bounds

The existing runner failure-metadata wiring described in `INTEGRATION.md` still
applies. Include hosted workflow/runtime/preparer and backend hashes in the frozen
protocol. Templates have local mocked/offline validation; hosted provisioning and
GitHub artifact behavior still need an authorized development smoke run. Oracle
logs/JUnit can expose failing test tracebacks, although hidden source and operator
metadata are absent from the target snapshot. The common CI machine remains an
execution environment for candidate Python, not a hostile-code sandbox.
