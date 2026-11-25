# Operator integration contract

This directory and `src/buildtrace/research_benchmark/` are **operator-only**.
Deploy only the new project directory returned by `materialize_case`. The
generator, catalogue, inverse edits, verifier bundle and operator metadata must
not be copied into an agent branch or made available through its source tools.

## Python API

```python
from pathlib import Path
from buildtrace.research_benchmark import (
    catalogue, materialize_case, materialize_baseline, verify_snapshot,
)

# Parents exist; both leaf directories must be new and disjoint.
task = materialize_case(
    "rv2-005", Path("scratch/opaque-project"),
    verifier_dir=Path("scratch/trusted-verifier"),
)
operator_metadata = task.to_dict()
result = verify_snapshot(task.destination, task.verifier_dir, work_dir="scratch")
assert not result.passed  # This is the initial broken candidate.

# In another new directory, obtain the correct seed for validation/reference.
healthy = materialize_baseline("rv2-005", Path("scratch/healthy-project"))
```

`materialize_case(..., correct_baseline=True)` is equivalent to
`materialize_baseline`. Every mutation validates a unique exact anchor, even
when creating a baseline. Missing/duplicate anchors raise `AnchorError` before
writing output. Existing destinations are refused. Generation is deterministic
and constructs an allowlisted tree from embedded source/fixtures; it does not
archive the workspace or read environment/configuration secrets.

`snapshot_files(id, correct_baseline=False)` returns the same relative-path/bytes
map without disk I/O. `reference_edits(id)` returns exact inverse `Edit` objects
for operator use. The optional Python-only `wrong_repair=` selects a **hypothetical
counterexample** from the operator catalogue, not a historical agent patch.
Inverse edits are convenient correct baselines, not exact-diff grading: alternative
repairs that satisfy the behavioral/packaging/protection contract are accepted.

CLI, from this repository using the existing environment:

```bash
".venv/Scripts/python.exe" -m buildtrace.research_benchmark catalog
".venv/Scripts/python.exe" -m buildtrace.research_benchmark materialize rv2-005 "scratch/opaque-project" --verifier-dir "scratch/trusted-verifier"
".venv/Scripts/python.exe" -m buildtrace.research_benchmark verify "scratch/opaque-project" --verifier-dir "scratch/trusted-verifier" --work-dir "scratch"
```

`materialize` prints operator JSON to stdout; it never writes that metadata
inside the project. `verify` exits 0 only for the complete passing contract,
1 for a failed verification. Catalogue names and paths are operator conveniences;
the coordinator can choose entirely opaque branch/directory names.

## Common agent project

All cases have:

- `pyproject.toml`, `.[dev]` with pytest/build, Python 3.11+;
- `src/bench_app/`, CLI `python -m bench_app examples/input.json`;
- `ci/constraints.txt`, identical stdlib-only PEP 517/660 build backend and
  `ci/check_distributions.py`;
- protected `tests/test_application.py`, public example input/output, nonsecret
  deployment/configuration/API fixtures under `context/`;
- `vendor/wheels/` for offline runtime dependencies (the service's synthetic
  wirekit v1/v2 wheels include their inspectable source in `context/dependencies/`);
- the identical `.github/workflows/ci.yml` workflow, with job `checks`.

The workflow assumes a preprovisioned offline Python runner, labelled
`buildtrace-offline-python311`, with pytest 8.3.5 and build 1.2.2.post1. The
existing repository `.venv` already has these tools. Provisioning the runner
and pinning checkout/setup action revisions belong to the trusted coordinator.
No project runtime needs a network connection. The common commands are:

```bash
python -m pip install --no-index --no-build-isolation --find-links vendor/wheels -c ci/constraints.txt -e '.[dev]'
python -m pytest -q tests
python -m build --no-isolation --wheel --sdist
python ci/check_distributions.py
python -m bench_app examples/input.json
```

The public distribution check exercises both a directly built wheel and a
wheel rebuilt from the sdist, away from the checkout. Thus the packaging cases
produce usable current diagnostics even without access to oracle-only tests.
Use a clean `dist/` for this public command. The local verifier instead builds
into fresh external temporary directories.

## Metadata and trusted verification

Schema: `buildtrace.research-benchmark.v2`. `MaterializedCase.to_dict()` has
`case_id`, `destination`, `verifier_dir`, `correct_baseline`, and `contract`.
The contract contains:

- `snapshot_sha256`: deterministic length-framed path/content fingerprint;
- `protected_sha256`: exact protected file hashes; source Python, pyproject and
  constraints are editable. New files are allowed only under the same source
  Python allowlist; unexpected noneditable files are rejected;
- `oracle_sha256`: hashes for external `tests/test_oracle.py` and
  `fixtures/checks.json`; the latter contains verifier-only requests/answers;
- **separate** `expected_public_tests` and `expected_oracle_tests`;
- install/test/build/distribution/CLI argv, editable/protected paths,
  `workflow`, identical `required_steps`, exact-count/zero-skip requirements;
- `required_phases`: `protected`, `resolve`, `public`, `build`, `cli`,
  `wheel_oracle`, `sdist_rebuild`, `sdist_oracle`.

The verifier directory contains its own `manifest.json`. Pin this manifest/hash
outside the candidate repository. `load_contract(verifier_dir)` reads it.
`verify_snapshot` supports a relocated candidate root and an explicitly supplied
trusted contract; it never relies on the original absolute output path.

Local verification uses the supplied/current Python, offline pip **dry-run**
resolution, preinstalled pytest/build, and protected local dependency wheels.
It does not install candidates into the shared `.venv`. Public tests run against
source. Both oracle runs import the actual candidate wheel, prove its import
origin, use an external working directory with no source `PYTHONPATH`, enforce
the exact count, and reject failures/errors/skips. A partial passing phase list
is not a verified repair. The returned `VerificationResult` contains every
executed phase's argv, return code, output, observed count and pass flag.

The coordinator should integrate:

1. `catalogue()["cases"][i]["experiment_fields"]` for `id`, **mechanism family**,
   category, split (`development` maps to core `dev`), `expected_test_count`, and
   an empty editable-environment list. Add deployed repository, immutable base
   SHA, initial failing run/capture, and trusted oracle SHA after deployment.
2. The returned contract's allowlists, protected hashes, `workflow` and
   `required_steps`. No experiment core changes are made by this generator.
3. Both count proofs. `TaskSpec.expected_test_count` maps to the **public** count;
   the oracle-only count is different and must be carried separately by the
   trusted verifier. Each of the two oracle runs has that oracle-only count,
   not a public-plus-oracle total.
4. Candidate-SHA checkout proof and trusted-workflow pinning in the remote
   verifier. This local utility has content hashes and import-origin proof,
   but does not manufacture GitHub run URLs, commits, captures or SHA attestations.

Candidate Python is executed during verification. Process/filesystem/network
isolation belongs to the execution environment; this local utility is not a
sandbox. It performs no publishing, API calls, or Git operations.
