"""Prepare and calibrate the 50-problem repository repair study without model calls."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
import json
from pathlib import Path
from random import Random

from buildtrace.research_benchmark.catalogue import get_case
from buildtrace.research_benchmark.materialize import materialize_case, snapshot_files, tree_sha256
from buildtrace.research_benchmark.model import Case
from buildtrace.research_benchmark.verify import load_contract, verify_snapshot


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data/local-patch-50"
GROUPS = (
    ("ingestion", (7, 8, 9, 10, 11, 12, 13, 14), 17),
    ("service", (19, 20, 21, 23, 24, 25, 26, 27, 28), 17),
    ("artifacts", (33, 34, 36, 37, 38, 39, 40, 41, 42), 16),
)


def case_groups():
    for domain, numbers, count in GROUPS:
        source_cases = [get_case(f"rv2-{number:03}") for number in numbers]
        triples = list(combinations(source_cases, 3))
        quads = list(combinations(source_cases, 4))
        rng = Random(f"local-patch-development-v2/{domain}")
        rng.shuffle(triples)
        rng.shuffle(quads)
        selections = triples[:count // 2] + quads[:count - count // 2]
        for index, sources in enumerate(selections, 1):
            case = Case(
                id=f"patch-{domain}-{index:02}", split="development", domain=domain,
                family=domain, title=f"{domain.capitalize()} regression {index:02}",
                mechanism="Multiple interacting deployment checks fail after a source change.",
                tags=("multi-defect",), edits=tuple(edit for source in sources for edit in source.edits),
            )
            yield case, sources


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_case(case, reference_hashes, work):
    directory = OUTPUT / "fixtures" / case.id
    directory.mkdir(exist_ok=True)
    snapshot = directory / "project"
    oracle = directory / "oracle"
    if not snapshot.exists() and not oracle.exists():
        materialize_case(case, snapshot, verifier_dir=oracle)
    contract = load_contract(oracle)
    expected = tree_sha256(snapshot_files(case))
    if contract.snapshot_sha256 != expected:
        raise ValueError(f"Saved snapshot does not match the study definition: {case.id}")
    reference = tree_sha256(snapshot_files(case, correct_baseline=True))
    if reference != reference_hashes[case.domain]:
        raise ValueError(f"Expected the same repaired seed for {case.id}")
    result = verify_snapshot(snapshot, oracle, contract=contract, work_dir=work, timeout=45)
    final = result.phases[-1]
    reproduced = not result.passed and final.returncode not in {None, 0} and final.phase != "protected"
    if not result.passed and final.phase in {"cli", "public", "wheel_oracle", "sdist_oracle"} and final.returncode == 0:
        reproduced = True
    if not reproduced:
        raise ValueError(f"Expected a reproducible check failure for {case.id}: {final.phase}\n{final.detail[-1500:]}")
    save_json(directory / "initial.json", {
        "case": case.id,
        "snapshot_sha256": expected,
        "first_failure": final.phase,
        "output": final.detail[-8000:],
    })
    return final.phase


def main():
    cases = list(case_groups())
    (OUTPUT / "fixtures").mkdir(parents=True, exist_ok=True)
    (OUTPUT / "reference").mkdir(exist_ok=True)
    work = OUTPUT / "scratch"
    work.mkdir(exist_ok=True)
    reference_hashes = {}
    for domain, _, _ in GROUPS:
        case = next(case for case, _ in cases if case.domain == domain)
        project = OUTPUT / "reference" / domain
        oracle = OUTPUT / "reference" / f"{domain}-oracle"
        if not project.exists() and not oracle.exists():
            materialize_case(case, project, correct_baseline=True, verifier_dir=oracle)
        result = verify_snapshot(project, oracle, work_dir=work, timeout=45)
        if not result.passed:
            failed = result.phases[-1]
            raise ValueError(f"Reference repair failed ({domain}, {failed.phase}): {failed.detail[-1800:]}")
        reference_hashes[domain] = tree_sha256(snapshot_files(case, correct_baseline=True))
        print(f"Reference passed offline verification: {domain}", flush=True)
    with ThreadPoolExecutor(max_workers=5) as pool:
        phases = []
        for index, phase in enumerate(pool.map(lambda case: verify_case(case, reference_hashes, work),
                                                [case for case, _ in cases]), 1):
            phases.append(phase)
            if index % 5 == 0:
                print(f"Reproduced {index}/{len(cases)} broken snapshots.", flush=True)
    spec = {
        "id": "50 development v2",
        "data_kind": "live",
        "validation": "local-repository",
        "trials": 3,
        "model": {"provider": "openai", "model": "gpt-6-luna", "temperature": None, "reasoning_effort": "low"},
        "tasks": [{"id": case.id, "family": case.family, "split": "dev", "domain": case.domain,
                   "title": case.title, "underlying_cases": [source.id for source in sources]}
                  for case, sources in cases],
        "calibration": {"reference_passes": sorted(reference_hashes), "first_failure_counts": dict(Counter(phases))},
    }
    directory = OUTPUT / "experiments" / "dev-v2"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "experiment.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != spec:
        raise ValueError("Study inputs differ from the existing experiment")
    if not path.exists():
        save_json(path, spec)
    (directory / "results.jsonl").touch(exist_ok=True)
    print(f"{spec['id']}: all {len(cases)} snapshots calibrated; {len(cases) * 9} model trials planned.", flush=True)


if __name__ == "__main__":
    main()
