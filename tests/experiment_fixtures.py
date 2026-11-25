from buildtrace.experiments.models import (
    AgentMode,
    Budget,
    ExperimentSpec,
    ModelSettings,
    RunRecord,
    TaskSpec,
    Timing,
    Verification,
    fingerprint,
)


def experiment_spec():
    return ExperimentSpec(
        id="synthetic-paired-test",
        data_kind="synthetic",
        model=ModelSettings(model="gpt-4.1"),
        budget=Budget(),
        corpus="corpus",
        corpus_sha256="a" * 64,
        insights_sha256="b" * 64,
        tasks=[
            TaskSpec(
                id=task_id,
                family=task_id,
                split="test",
                category="test",
                repository="example/invoice",
                base_sha="c" * 40,
                initial_run_url="https://github.com/example/invoice/actions/runs/12345/attempts/1",
                capture_id="d" * 32,
            )
            for task_id in ("easy", "hard")
        ],
    )


def run_record(spec, task_id, mode, trial, success, **changes):
    task = next(task for task in spec.tasks if task.id == task_id)
    verification = Verification(
        attempt=1,
        candidate_sha="e" * 40,
        run_url="https://github.com/example/invoice/actions/runs/23456/attempts/1",
        workflow_conclusion="success" if success else "failure",
        required_checks_passed=success,
        protected_files_unchanged=True,
        oracle_passed=success,
        elapsed_seconds=5,
    )
    fields = dict(
        id=f"{task_id}-{mode.value}-{trial}",
        experiment_id=spec.id,
        experiment_sha256=fingerprint(spec),
        protocol_sha256="f" * 64,
        task_id=task.id,
        task_sha256=fingerprint(task),
        mode=mode,
        trial=trial,
        model=spec.model,
        budget=spec.budget,
        corpus_sha256=spec.corpus_sha256 if mode != AgentMode.NO_MEMORY else None,
        insights_sha256=spec.insights_sha256 if mode != AgentMode.NO_MEMORY else None,
        outcome="solved" if success else "unsolved",
        stop_reason="verified" if success else "attempt limit",
        timing=Timing(wall_seconds=10 + trial, ci_wait_seconds=5),
        verifications=[verification],
        trajectory_path="synthetic-turns.jsonl",
    )
    return RunRecord(**(fields | changes))


def paired_records(spec):
    outcomes = {
        AgentMode.NO_MEMORY: {"easy": [True, True, True], "hard": [False, False, False]},
        AgentMode.INSIGHTS_ONLY: {"easy": [True, False, False], "hard": [False, True, False]},
        AgentMode.WIKITRACE: {"easy": [True, True, False], "hard": [True, True, False]},
    }
    return [
        run_record(spec, task_id, mode, trial, success)
        for mode, tasks in outcomes.items()
        for task_id, trials in tasks.items()
        for trial, success in enumerate(trials)
    ]
