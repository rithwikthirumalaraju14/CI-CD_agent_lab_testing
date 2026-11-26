"""Admit only verified solutions for the five failed learning questions.

This script never edits the original learning-50 corpus or its source results.
"""

import hashlib
import json
from pathlib import Path


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "CI-CD_agent_lab_testing").is_dir()
            and (parent / "data").is_dir())
ORIGINAL = ROOT / "CI-CD_agent_lab_testing/output/learning50-final/memory-learning50-v1/corpus.json"
GROUPS = (
    (ROOT / "data/learning50-v2-five/experiments", ("V2L06", "V2L45")),
    (ROOT / "data/learning-failed-v3/experiments", ("V3L25", "V3L27", "V3L30")),
)


def main() -> None:
    ledger = {"original_corpus_sha256": hashlib.sha256(ORIGINAL.read_bytes()).hexdigest(), "tasks": []}
    for directory, task_ids in GROUPS:
        source = directory / "results.jsonl"
        rows = [(line, json.loads(line)) for line in source.read_text(encoding="utf-8").splitlines() if line]
        admitted = []
        for task_id in task_ids:
            attempts = sorted((row for row in rows if row[1]["task_id"] == task_id), key=lambda row: row[1]["trial"])
            if len(attempts) != 3 or [row[1]["trial"] for row in attempts] != [0, 1, 2]:
                raise ValueError(f"{task_id}: expected exactly three independent trials")
            verified = []
            for line, record in attempts:
                checks = record["verifications"][-1] if record["verifications"] else {}
                passed = (
                    record["mode"] == "no-memory"
                    and record["outcome"] == "solved"
                    and checks.get("workflow_conclusion") == "success"
                    and checks.get("required_checks_passed") is True
                    and checks.get("oracle_passed") is True
                    and checks.get("protected_files_unchanged") is True
                )
                verified.append(passed)
                if passed:
                    admitted.append(line)
            if not any(verified):
                raise ValueError(f"{task_id}: 0/3 verified solutions; do not build memory")
            ledger["tasks"].append({"id": task_id, "verified_trials": sum(verified), "attempts": 3,
                                    "source_results": str(source.relative_to(ROOT))})
        target = directory / "admitted-results.jsonl"
        target.write_text("\n".join(admitted) + "\n", encoding="utf-8")
        ledger.setdefault("admitted_records", []).append({"path": str(target.relative_to(ROOT)), "count": len(admitted),
                                                          "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    target = ROOT / "deliverables/FAILED_FIVE_ADMISSION.json"
    target.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(ledger, indent=2))


if __name__ == "__main__":
    main()
