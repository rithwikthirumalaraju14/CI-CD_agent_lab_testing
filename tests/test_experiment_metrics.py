import pytest
from pydantic import ValidationError

from buildtrace.experiments.metrics import build_report, render_markdown
from buildtrace.experiments.models import (
    AgentMode,
    ChangedHunk,
    Decision,
    EvidenceCitation,
    EvidenceDecision,
    RunRecord,
    fingerprint,
)

from experiment_fixtures import experiment_spec, paired_records


def test_gain_and_regression_have_conditional_denominators():
    spec = experiment_spec()
    report = build_report(spec, paired_records(spec), resamples=100)
    assert report["baseline_solved_tasks"] == 1
    assert report["baseline_unsolved_tasks"] == 1
    insights = report["methods"]["insights-only"]
    wiki = report["methods"]["wikitrace"]
    assert insights["gain"] == {"numerator": 1, "denominator": 3, "percent": 100 / 3}
    assert insights["regression"] == {"numerator": 2, "denominator": 3, "percent": 200 / 3}
    assert wiki["successful_fixes"]["numerator"] == 4
    assert wiki["gain"]["numerator"] == 2
    assert wiki["regression"]["numerator"] == 1
    assert report["methods"]["no-memory"]["gain"] is None
    assert "Synthetic data" in render_markdown(report)


def test_v2_display_does_not_promote_syntactic_changes_to_reviewed_adopted_effects():
    spec = experiment_spec()
    report = build_report(spec, paired_records(spec), resamples=0)
    report.update(schema_version=2, split="dev")
    for method in report["methods"].values():
        method["evidence_mechanism"] = {"fully_reviewed_bursts": 0, "reading_bursts": 0, "reviewed_adopted_effects": 0}
    wiki = report["methods"]["wikitrace"]
    wiki["evidence_impact"]["decision_changes"] = {"numerator": 1, "denominator": 1, "percent": 100}
    wiki["evidence_mechanism"] = {"fully_reviewed_bursts": 1, "reading_bursts": 1, "reviewed_adopted_effects": 0}
    text = render_markdown(report)
    assert "Reviewed adopted effects" in text and "0/1 reviewed" in text
    assert "100.0% (1/1)" not in text
    wiki["evidence_mechanism"]["fully_reviewed_bursts"] = 0
    assert "N/A (1 unreviewed)" in render_markdown(report)


def test_incomplete_or_mixed_experiments_are_rejected():
    spec = experiment_spec()
    records = paired_records(spec)
    with pytest.raises(ValueError, match="Incomplete comparison"):
        build_report(spec, records[:-1], resamples=0)
    with pytest.raises(ValueError, match="Duplicate"):
        build_report(spec, records + [records[0]], resamples=0)
    changed = records[-1].model_copy(update={"insights_sha256": "f" * 64})
    with pytest.raises(ValueError, match="identical frozen insights"):
        build_report(spec, records[:-1] + [changed], resamples=0)
    changed = records[-1].model_copy(update={"experiment_sha256": "f" * 64})
    with pytest.raises(ValueError, match="different experiment"):
        build_report(spec, records[:-1] + [changed], resamples=0)


def test_no_eligible_gain_is_na_and_infra_errors_are_visible():
    spec = experiment_spec()
    records = paired_records(spec)
    for index, record in enumerate(records):
        if record.mode == AgentMode.NO_MEMORY and record.task_id == "hard":
            data = record.model_dump()
            data.update(outcome="infra-error", verifications=[], stop_reason="CI unavailable")
            records[index] = RunRecord.model_validate(data)
    report = build_report(spec, records, resamples=50)
    assert report["baseline_unscorable_tasks"] == ["hard"]
    assert report["methods"]["wikitrace"]["gain"]["percent"] is None
    assert report["methods"]["wikitrace"]["gain"]["denominator"] == 0
    assert report["methods"]["no-memory"]["outcomes"]["infra-error"] == 3


def test_unreviewed_changes_are_not_called_unnecessary_or_zero():
    spec = experiment_spec()
    records = paired_records(spec)
    hunk = ChangedHunk(id="hunk-1", path="src/app.py", added_lines=1, deleted_lines=1, diff="-a\n+b")
    records[-1] = records[-1].model_copy(update={"hunks": [hunk]})
    changes = build_report(spec, records, resamples=0)["methods"]["wikitrace"]["changes"]
    assert changes["changed_lines"] == 2
    assert changes["unnecessary_hunks"] is None
    assert changes["known_unnecessary_hunks"] == 0
    reviewed = hunk.model_copy(update={"classification": "unnecessary", "reviewer": "human-1"})
    records[-1] = records[-1].model_copy(update={"hunks": [reviewed]})
    changes = build_report(spec, records, resamples=0)["methods"]["wikitrace"]["changes"]
    assert changes["unnecessary_hunks"] == 1
    assert changes["unnecessary_lines"] == 2


def test_evidence_impact_ignores_wording_changes_and_tracks_incomplete_notes():
    spec = experiment_spec()
    records = paired_records(spec)
    before = Decision(action="apply", target="dependency", parameters={"version": "2"}, summary="Upgrade")
    citation = EvidenceCitation(
        insight_id="insight-0000", source_run_id="step-0000", layer="raw",
        document="/work/corpus/raw/step-0000/solver.full.jsonl", first_line=1, last_line=2,
        document_sha256="a" * 64,
    )
    events = [
        EvidenceDecision(id="read-1", question="Does it apply?", trigger="uncertainty", before=before,
                         after=before.model_copy(update={"summary": "Use the newer version"}), citations=[citation]),
        EvidenceDecision(id="read-2", question="Which version?", trigger="environment-difference", before=before,
                         after=Decision(action="reject", target="dependency"), citations=[citation]),
        EvidenceDecision(id="read-3", question="What happened?", trigger="failed-source", before=before, citations=[citation]),
    ]
    records[-1] = records[-1].model_copy(update={"evidence_decisions": events})
    impact = build_report(spec, records, resamples=0)["methods"]["wikitrace"]["evidence_impact"]
    assert impact["lookups"] == 3
    assert impact["completed_before_after"] == 2
    assert impact["decision_changes"]["percent"] == 50
    assert impact["review_confirmed_changes"]["percent"] is None


def test_success_requires_actual_passing_verification_and_access_is_scoped():
    spec = experiment_spec()
    record = paired_records(spec)[0].model_dump()
    with pytest.raises(ValidationError, match="passing final verification"):
        RunRecord.model_validate(record | {"verifications": []})
    with pytest.raises(ValidationError, match="must not receive historical memory"):
        RunRecord.model_validate(record | {"corpus_sha256": "a" * 64})
    with pytest.raises(ValidationError, match="independent reviewer"):
        ChangedHunk(id="h", path="app.py", added_lines=1, deleted_lines=0, diff="+x", classification="unnecessary")


def test_task_level_bootstrap_is_reproducible_and_does_not_invent_reference_floor():
    spec = experiment_spec()
    first = build_report(spec, paired_records(spec), resamples=100)["paired_bootstrap"]
    second = build_report(spec, paired_records(spec), resamples=100)["paired_bootstrap"]
    assert first == second
    differences = first["task_level_differences"]
    assert differences["wikitrace-minus-no-memory"]["gain"]["ci95_low"] is None
    assert differences["wikitrace-minus-insights-only"]["gain"]["eligible_resamples"] > 0
