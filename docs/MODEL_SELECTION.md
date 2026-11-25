# Pilot model: GPT-6 Luna

The pilot uses `gpt-6-luna` for the no-memory agent, insights-only agent,
WikiTrace agent, and historical-session compiler.

## Verified model information

Checked against OpenAI documentation on November 16, 2026:

| Model | Standard input / 1M tokens | Standard output / 1M tokens |
| --- | ---: | ---: |
| GPT-6 Luna | $0.10 | $0.50 |
| GPT-4.1 mini | $0.40 | $1.60 |

The GPT-4.1 mini page marks that model deprecated. GPT-6 Luna supports function
calling, structured outputs, and reasoning. Its documented API ID is
`gpt-6-luna`.

The pilot uses the Responses API because Luna supports reasoning with tools
there. Chat Completions supports Luna function calling only when reasoning
effort is `none`.

## Fixed pilot settings

```json
{
  "provider": "openai",
  "model": "gpt-6-luna",
  "temperature": null,
  "reasoning_effort": "medium"
}
```

Temperature is omitted from API requests because it is unsupported with enabled
reasoning. Every method receives the same reasoning effort and budget. Each
Responses conversation is local to one trial, with `store: false`; replayable
reasoning items are preserved within that trial and never shared between trials.

The client records actual input, cached-input, output, and reasoning-token usage.
Reasoning tokens are already included in output tokens and are not counted twice.
Per-token prices alone do not establish total per-task cost or debugging quality.

The documented alias currently has no separate dated snapshot listed. The runner
records the model ID actually returned by the API and rejects a comparison that
mixes different returned model IDs. API account access is checked when live calls
are made.

Whether this model benefits from evidence-linked memory is measured on the CI
tasks; lower prices and a newer model name do not establish that result.

## Sources

- [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna).
- [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini).
- [Using GPT-6: API and model parameters](https://developers.openai.com/api/docs/guides/latest-model#migration-quickstart).
- [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning).
