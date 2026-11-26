"""Combine the two admitted failed-question corpora without touching learning-50 v1."""

import hashlib
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

from wikitrace.memory.insights import InsightItem, render_inject, render_insight_index

from buildtrace.experiments.memory import FrozenMemory


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "CI-CD_agent_lab_testing").is_dir()
            and (parent / "data").is_dir())
BASE = ROOT / "CI-CD_agent_lab_testing/output/learning50-final"
SOURCES = (BASE / "memory-failed-five-v2-two", BASE / "memory-failed-five-v3-three")
TARGET = BASE / "memory-failed-five-combined"
ORIGINAL = BASE / "memory-learning50-v1/corpus.json"


def rewrite(text: str, step_offset: int, insight_offset: int) -> str:
    text = re.sub(r"step-(\d{4})", lambda match: f"step-{int(match[1]) + step_offset:04d}", text)
    return re.sub(r"insight-(\d{4})", lambda match: f"insight-{int(match[1]) + insight_offset:04d}", text)


def index_entries(path: Path) -> tuple[str, list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    first = next(index for index, line in enumerate(lines) if re.match(r"^\d+\. \[step-\d+\]", line))
    return "\n".join(lines[:first]), [re.sub(r"^\d+\. ", "", line) for line in lines[first:] if re.match(r"^\d+\. \[step-\d+\]", line)]


def main() -> None:
    if TARGET.exists():
        raise ValueError(f"Choose a new output; {TARGET} already exists")
    corpora = [FrozenMemory(path) for path in SOURCES]
    first, second = corpora
    if any(row["outcome"] != "solved" for corpus in corpora for row in corpus.manifest["training_tasks"]):
        raise ValueError("Only verified solved sessions may enter this supplement")
    if first.manifest["compiler_model"] != second.manifest["compiler_model"] or first.manifest["upstream"] != second.manifest["upstream"]:
        raise ValueError("Incompatible compiler model or WikiTrace revision")
    if {row["id"] for corpus in corpora for row in corpus.manifest["training_tasks"]} != {"V2L06", "V2L45", "V3L25", "V3L27", "V3L30"}:
        raise ValueError("Supplement must contain exactly the five repaired question IDs")
    step_offset = len(first.manifest["training_tasks"])
    insight_offset = len(first.items)
    stage = TARGET.parent / ".pending-failed-five-combined"
    if stage.exists():
        raise ValueError(f"Pending build exists: {stage}")
    shutil.copytree(SOURCES[0], stage)
    try:
        files = dict(first.manifest["files"])
        for old_path in second.manifest["files"]:
            if old_path in {"insight/_index.md", "compilation/_index.md", "raw/_index.md"}:
                continue
            new_path = rewrite(old_path, step_offset, insight_offset)
            if new_path in files:
                raise ValueError(f"Duplicate corpus path: {new_path}")
            content = (SOURCES[1] / old_path).read_bytes()
            if old_path.endswith(".md"):
                content = rewrite(content.decode("utf-8"), step_offset, insight_offset).encode("utf-8")
            destination = stage / new_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            files[new_path] = hashlib.sha256(content).hexdigest()
        for layer in ("compilation", "raw"):
            header, left = index_entries(SOURCES[0] / layer / "_index.md")
            _, right = index_entries(SOURCES[1] / layer / "_index.md")
            entries = left + [rewrite(line, step_offset, insight_offset) for line in right]
            content = header + "\n" + "\n".join(f"{index}. {entry}" for index, entry in enumerate(entries, 1)) + "\n"
            relative = f"{layer}/_index.md"
            encoded = content.encode("utf-8")
            (stage / relative).write_bytes(encoded)
            files[relative] = hashlib.sha256(encoded).hexdigest()
        new_items = [replace(item, id=rewrite(item.id, step_offset, insight_offset),
                             source_step=rewrite(item.source_step, step_offset, insight_offset)) for item in second.items]
        items = [*first.items, *new_items]
        content = render_insight_index(items)
        encoded = content.encode("utf-8")
        (stage / "insight/_index.md").write_bytes(encoded)
        files["insight/_index.md"] = hashlib.sha256(encoded).hexdigest()
        manifest = dict(first.manifest)
        manifest["training_tasks"] = [*first.manifest["training_tasks"],
                                      *[row | {"source_step": rewrite(row["source_step"], step_offset, insight_offset)}
                                        for row in second.manifest["training_tasks"]]]
        manifest["insights"] = [*first.manifest["insights"],
                                *[item | {"id": rewrite(item["id"], step_offset, insight_offset),
                                          "source_step": rewrite(item["source_step"], step_offset, insight_offset)}
                                  for item in second.manifest["insights"]]]
        manifest["insights_sha256"] = hashlib.sha256(render_inject(items, source=True).encode("utf-8")).hexdigest()
        manifest["compiler_calls"] = [*first.manifest["compiler_calls"], *second.manifest["compiler_calls"]]
        manifest["training_admission_ledger"] = [*first.manifest["training_admission_ledger"],
                                                  *second.manifest["training_admission_ledger"]]
        provenance = {"source_corpus_hashes": [corpus.sha256 for corpus in corpora],
                      "original_learning50_corpus_sha256": hashlib.sha256(ORIGINAL.read_bytes()).hexdigest(),
                      "step_offset": step_offset, "insight_offset": insight_offset,
                      "policy": "Only verified solutions for V2L06, V2L45, V3L25, V3L27, V3L30; original learning50 corpus untouched."}
        content = json.dumps(provenance, indent=2) + "\n"
        encoded = content.encode("utf-8")
        (stage / "merge-provenance.json").write_bytes(encoded)
        files["merge-provenance.json"] = hashlib.sha256(encoded).hexdigest()
        manifest["files"] = files
        (stage / "corpus.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        stage.rename(TARGET)
        merged = FrozenMemory(TARGET)
        if hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() != provenance["original_learning50_corpus_sha256"]:
            raise ValueError("Original learning-50 corpus changed")
        print(json.dumps({"corpus": str(TARGET), "corpus_sha256": merged.sha256,
                          "insights_sha256": merged.insights_sha256,
                          "runs": len(merged.manifest["training_tasks"]), "insights": len(merged.items)}, indent=2))
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise


if __name__ == "__main__":
    main()
