import json

import httpx
import pytest

from buildtrace.evidence import EvidenceError, EvidenceStore
from buildtrace.experiments.memory import FrozenMemory, UPSTREAM_REVISION, build_memory
from buildtrace.experiments.models import AgentMode, ExperimentSpec, ModelSettings, TaskSpec
from buildtrace.experiments.openai import OpenAIClient

from experiment_fixtures import run_record


@pytest.mark.parametrize("success", [True, False])
def test_real_upstream_compiler_and_renderers_build_linked_ci_memory(tmp_path, collected_run, success):
    store = EvidenceStore(tmp_path / "evidence")
    capture = store.save(collected_run)
    task = TaskSpec(
        id="historical-import", family="version-import", split="train", category="import",
        repository="example/invoice", base_sha=collected_run.source_ref,
        initial_run_url=capture["run_url"], capture_id=capture["id"],
    )
    spec = ExperimentSpec(id="synthetic-training", data_kind="synthetic", model=ModelSettings(model="gpt-4.1"), tasks=[task])
    record = run_record(spec, task.id, AgentMode.NO_MEMORY, 0, success, trajectory_path="source.jsonl")
    source = b'{"type":"function_call","name":"read_current_log","result":"Installed version 1; import failed"}\n'
    (tmp_path / "source.jsonl").write_bytes(source)
    requests = []
    output = {
        "title": "Version-specific import", "description": "A dependency API differed by version.",
        "candidate_insights": [{"title": "Check installed API", "guideline": "Check the installed dependency version before changing it.", "applicability": "When the import belongs to a versioned dependency."}],
        "key_decisions" if success else "failure_causes": [{"decision": "Used an unavailable import", "support": "The recorded import failed with version 1 installed."}],
    }

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "compiler-1", "model": "gpt-4.1-pinned",
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(output)}]}],
            "usage": {"input_tokens": 200, "output_tokens": 50},
        })

    transport = OpenAIClient(spec.model, api_key="test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    memory = build_memory(spec, [record], tmp_path, store, tmp_path / "corpus", transport)
    assert memory.manifest["upstream"]["revision"] == UPSTREAM_REVISION
    assert memory.manifest["data_kind"] == "synthetic"
    assert "Check installed API" in memory.insights_text
    assert memory.items[0].outcome == ("success" if success else "failure")
    assert "expert in Python CI" in requests[0]["input"][0]["content"]
    schema = requests[0]["text"]["format"]["schema"]
    assert "candidate_insights" in schema["properties"]
    compilation, citation = memory.read("insight-0000", "compilation")
    assert "/work/corpus/insight/insight-0000.md" in compilation["text"]
    assert citation.source_run_id == "step-0000"
    raw, _ = memory.read("insight-0000", "raw", "solver.full.jsonl")
    assert raw["text"].encode() == source
    assert (tmp_path / "corpus/raw/step-0000/initial-ci/raw/logs.zip").read_bytes() == collected_run.log_archive
    loaded = FrozenMemory(tmp_path / "corpus", memory.sha256)
    assert loaded.insights_sha256 == memory.insights_sha256
    with pytest.raises(ValueError):
        memory.read("insight-0000", "raw", "../../outside")
    (tmp_path / "corpus/raw/step-0000/solver.full.jsonl").write_text("changed", encoding="utf-8")
    with pytest.raises(EvidenceError, match="checksum"):
        memory.read("insight-0000", "raw", "solver.full.jsonl")
