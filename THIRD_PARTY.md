# Upstream components

## WikiTrace

- Repository: <https://github.com/yukyeongleee/wikitrace>
- Pinned revision: `25a83679af0eb67ab3c835ab920d3a243fb80845`
- License: [Apache License 2.0](https://github.com/yukyeongleee/wikitrace/blob/25a83679af0eb67ab3c835ab920d3a243fb80845/LICENSE)
- Paper: Yukyeong Lee, Yumin Choi, Jinheon Baek, and Sung Ju Hwang,
  *Don't Just Trust Your Memory: Evidence-Linked Memory Reduces Memory-Induced
  Degradation in LLM Agents* (2026).

BuildTrace declares WikiTrace as a commit-pinned Python dependency and directly
uses these upstream components:

- `memory.compiler.compile_trajectory` and `relink_compilation`.
- `harness.llm.client.LLMClient` for structured compiler generation.
- `memory.insights.InsightItem`, insight document/index rendering, and
  `render_inject` for identical insight text in both memory-enabled modes.
- `memory.store` compilation rendering, raw indexes, and corpus document shapes.

The CI adapter lives in `src/buildtrace/experiments/memory.py`. It supplies a
CI-specific `PromptProfile`, raw-session description, actual trial outcomes,
original CI evidence, and source links. The transport adapter connects the
upstream compiler interface to OpenAI's Responses API.

`src/buildtrace/experiments/prompts.py` adapts the selective-descent rules from
upstream `memory/prompts/descend.j2` to BuildTrace's read-only historical-evidence
tool. Execution uses the same BuildTrace GitHub Actions solver loop in all three
modes. This is a CI-domain adaptation of the WikiTrace memory method.

The original paper's benchmark scores are background research results. BuildTrace
reports its own measured CI repair outcomes only after live experiments run.
