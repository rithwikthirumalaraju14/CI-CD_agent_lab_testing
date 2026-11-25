"""OpenAI transport shared by all solver modes and upstream WikiTrace's compiler."""

import os
from dataclasses import dataclass, field

import httpx

from buildtrace.experiments.models import ModelSettings


class OpenAIError(RuntimeError):
    """An API/configuration failure, distinct from an unsuccessful repair."""


@dataclass(frozen=True)
class ModelTurn:
    message: dict
    input_tokens: int
    output_tokens: int
    returned_model: str
    raw: dict
    continuation_items: list[dict] = field(default_factory=list)
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    stop_reason: str | None = None


class OpenAIClient:
    def __init__(self, settings: ModelSettings, api_key: str | None = None, client: httpx.Client | None = None):
        self.settings = settings
        self.api_key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        if not self.api_key:
            raise OpenAIError("Configure OPENAI_API_KEY locally before running live agents or building memory.")
        self.client = client or httpx.Client()
        self._owns_client = client is None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if self._owns_client:
            self.client.close()

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        timeout: float = 120,
    ) -> ModelTurn:
        payload = {
            "model": self.settings.model,
            "input": messages,
            "max_output_tokens": max_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if self.settings.temperature is not None:
            payload["temperature"] = self.settings.temperature
        if self.settings.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.settings.reasoning_effort}
        if tools:
            response_tools = [{"type": "function", **tool["function"], "strict": False} for tool in tools]
            payload.update(tools=response_tools, parallel_tool_calls=False)
        if response_format is not None:
            if response_format.get("type") != "json_schema":
                raise OpenAIError("The compiler requires a JSON-schema response format")
            payload["text"] = {"format": {"type": "json_schema", **response_format["json_schema"]}}
        try:
            response = self.client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=max(0.01, timeout),
            )
        except httpx.RequestError as exc:
            raise OpenAIError("The OpenAI request failed or timed out.") from exc
        if response.status_code >= 400:
            raise OpenAIError(
                f"OpenAI returned HTTP {response.status_code}. Check the key, model, settings, and API quota."
            )
        try:
            raw = response.json()
            usage = raw["usage"]
            returned_model = raw["model"]
            input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
            reasoning_tokens = usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)
            cached_tokens = usage.get("input_tokens_details", {}).get("cached_tokens", 0)
            output = raw["output"]
            if not isinstance(output, list) or not isinstance(returned_model, str) or not returned_model:
                raise ValueError("Invalid output/model")
            if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens, reasoning_tokens, cached_tokens)):
                raise ValueError("Invalid usage")
            if reasoning_tokens > output_tokens or cached_tokens > input_tokens:
                raise ValueError("Invalid token breakdown")
            text, calls = [], []
            for item in output:
                if item["type"] == "function_call":
                    if not all(isinstance(item.get(key), str) for key in ("call_id", "name", "arguments")):
                        raise ValueError("Invalid function call")
                    calls.append({"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}})
                elif item["type"] == "message":
                    for part in item.get("content", []):
                        if part["type"] == "output_text":
                            text.append(part["text"])
                        elif part["type"] == "refusal":
                            text.append(part["refusal"])
                elif item["type"] != "reasoning":
                    raise ValueError("Unexpected unadvertised tool output")
            message = {"role": "assistant", "content": "\n".join(text) or None}
            if calls:
                message["tool_calls"] = calls
            stop_reason = None
            if raw.get("status") == "incomplete":
                stop_reason = str(raw.get("incomplete_details", {}).get("reason", "incomplete-response"))
            elif raw.get("status") != "completed":
                raise ValueError("Unsuccessful response status")
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise OpenAIError("OpenAI returned an invalid response or omitted usage needed for the experiment budget.") from exc
        return ModelTurn(message, input_tokens, output_tokens, returned_model, raw, output, reasoning_tokens, cached_tokens, stop_reason)

    def chat(
        self, model, messages, temperature=0, max_tokens=2048,
        response_format=None, reasoning_effort=None,
    ):
        """The transport interface expected by WikiTrace's real LLMClient."""
        from wikitrace.harness.llm.openrouter import ChatResponse

        if model != self.settings.model or temperature != (self.settings.temperature if self.settings.temperature is not None else 0):
            raise OpenAIError("The compiler attempted to change the experiment's configured model settings.")
        turn = self.complete(messages, max_tokens=max_tokens, response_format=response_format)
        if turn.stop_reason:
            raise OpenAIError(f"The compiler's model output was incomplete: {turn.stop_reason}")
        content = turn.message.get("content")
        if not isinstance(content, str):
            raise OpenAIError("The compiler received no structured text response.")
        # The upstream usage parser uses Chat-style keys; preserve Responses details as well.
        compiler_raw = turn.raw | {"usage": turn.raw["usage"] | {
            "prompt_tokens": turn.input_tokens, "completion_tokens": turn.output_tokens,
            "prompt_tokens_details": {"cached_tokens": turn.cached_input_tokens},
            "completion_tokens_details": {"reasoning_tokens": turn.reasoning_tokens},
        }}
        return ChatResponse(
            content=content,
            raw=compiler_raw,
            generation_id=str(turn.raw.get("id", "")),
            model_returned=turn.returned_model,
            finish_reason="stop",
            provider="openai",
        )
