# CI/CD agent memory lab

This repository hosts 50 independent CI/CD repair problems for the learning phase of an agent memory experiment. Each `buildtrace-inputs/learning-LNN` branch contains one broken project, its current contract, public tests, and a GitHub Actions workflow. The operator keeps reference fixes and hidden checks outside agent-visible task branches.

The [learning question list](LEARNING_50_QUESTIONS.md) names every task, and the [frozen baselines](LEARNING_50_BASELINES.md) link to each broken branch and initial failed CI run. For each one, the agent should inspect the failing CI run and repair the project without editing protected tests, fixtures, examples, or verifier files. The allowed repair surface is Python application source, `pyproject.toml`, and `ci/constraints.txt`.

The intended calibration is three independent no-memory attempts per problem with the same model settings. These 150 attempts have **not** yet been run; authored questions and a failing initial CI run do not count as training. A task's measured no-memory solve rate will be reported after those attempts. Learning outcomes will be frozen before any separate evaluation tasks are shown to memory-assisted agents.

These are offline Python projects that model CI failures involving dependency versions, packaging, artifacts, and deployment decisions. The release pipeline cases exercise decision logic in source code; they are not arbitrary YAML workflow editing tasks.
