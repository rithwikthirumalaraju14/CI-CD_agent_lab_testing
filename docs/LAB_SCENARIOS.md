# CI lab scenarios

This catalog is for the experiment operator. Keep it, reference repairs, and
other agents' repair branches outside the debugging agent's task context.

Target repository:
<https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab>

## Healthy baseline

- Branch: `main`
- Initial revision: `c6906b43c1d27598d52da2ecc4950018aa2215ad`
- Initial passing workflow: <https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43809926047>
- Local verification: 36 tests pass; package builds; example total is `99.00`.

## case-001

- Category: application test failure.
- Branch: `case-001`
- Failing revision: `c7de58d8094fe4c2c5a952c0eb9888c417eb39f6`
- Workflow: <https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43810145176>
- Mutation: the final total uses the subtotal instead of the amount remaining
  after the discount. Tax calculation, dependencies, workflow, and tests are unchanged.
- Expected failure: 5 tests fail, 31 pass; the example's total is `109.00`
  instead of `99.00`.
- Success condition: restore correct invoice behavior and pass all 36 original
  tests without weakening them.

### Reproduce locally

In a checkout of the **lab** repository, with its development environment installed:

```sh
git switch case-001
python -m pytest
```

Return to the healthy branch:

```sh
git switch main
```

### Capture the failure in BuildTrace

From the **agent** repository:

```sh
python -m buildtrace inspect https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43810145176
python -m buildtrace serve
```

Open <http://127.0.0.1:8000> and select the saved capture. Record its capture ID
and verify the archive with `python -m buildtrace verify CAPTURE_ID`.

For a repair attempt, branch from the fixed failing revision into a new repair
branch. Run CI on the new patched commit; re-running the original failed run
would execute the original code.

## Published comparison pilot

The trusted workflow revision is
`ab6ed493adf94149b45fd742e873cad8d2985447`. Its
[healthy CI](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43818836024)
passes all 36 original tests. `main` remains healthy; each case below is an
isolated branch. Branch names and commit messages use neutral case IDs.

| Case | Split | Category | Operator-only scenario | Initial failed run |
| --- | --- | --- | --- | --- |
| case-002 | train | test | Tax uses the subtotal instead of the discounted amount | [43819902392](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43819902392) |
| case-003 | train | dependency | CI pins Pydantic 1 while the application requires version 2 | [43820019210](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43820019210) |
| case-004 | train | import | Pydantic 1 is installed for code using the version-2 field-validator API | [43820094774](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43820094774) |
| case-005 | train | configuration | The required currency variable is absent from the job | [43820160917](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43820160917) |
| case-001 | test | test | Final total omits the discount | [43810145176](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43810145176) |
| case-006 | test | dependency | The project declares a version-1 range while CI pins version 2 | [43820255328](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43820255328) |
| case-007 | test | import | A local pydantic.py hides the correctly installed dependency | [43820332434](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43820332434) |
| case-008 | test | configuration | Currency is present but its value is unsupported | [43820393967](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43820393967) |

The exact failing revisions and local archive IDs are in
`experiments/training.json` and `experiments/evaluation.json`. The configurations
have disjoint task IDs, source revisions, and scenario-family labels. Fresh
checkouts can recreate archives with `experiment capture-inputs`.

All eight failures were checked against real CI evidence. Dependency cases fail
at installation; import and calculation cases fail at the test step;
configuration cases fail when running the application example. In particular:

- case-004 reports `cannot import name 'field_validator' from 'pydantic'` with
  the installed package path.
- case-007 reports missing Pydantic symbols from the repository's own
  `pydantic.py` file.
- case-005 reports a missing `INVOICE_CURRENCY`.
- case-008 reports that `INVOICE_CURRENCY` must be one of the supported values.

These pairs test applicability without assuming that an agent will follow a
misleading lesson or that WikiTrace will outperform the baselines.

## Trusted-backend operator check

A known repair of case-001 was used to check the real branch, dispatch,
verification, and archival path. This was an operator check with no LLM calls;
it is not included in agent-performance records or historical training memory.

- Candidate: `b784c11fdb955702e422457962010934f1e34221`.
- [Candidate CI](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43819102110).
- [Independent oracle](https://github.com/bluepal-rithwik-thirumalaraju/buildtrace-ci-lab/actions/runs/43819178298).
- Both passed; the oracle's JUnit report accounted for all 36 tests and its
  environment artifact proved the candidate checkout.
- Both captures' file checksums verified, including the retained oracle artifact.

## Expansion

After the pilot pipeline is validated with live agents, expand the predeclared
held-out set beyond four cases, including harder multi-step failures, conflicting
lessons, failed historical attempts, and unseen projects. Keep manually authored
misleading-memory stress tests separate from naturally compiled main results.
