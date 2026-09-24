"""Model access with the controls every agent needs: timeouts, a call budget, bounded retries,
schema-constrained JSON output, and token accounting for cost/latency reporting."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger("aeo.llm")

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model could not produce a usable answer within the retry limit."""


class BudgetExceeded(LLMError):
    pass


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0


class ChatModel(Protocol):
    label: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult: ...


class OllamaChat:
    """Ollama /api/chat. `format` constrains decoding to a JSON schema (structured outputs)."""

    def __init__(self, base_url: str, model: str, timeout_s: int) -> None:
        self.base_url = base_url
        self.model = model
        self.label = f"Local model · {model} via Ollama"
        self._timeout = httpx.Timeout(timeout_s, connect=5)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_ctx": 8192},
        }
        if schema is not None:
            payload["format"] = schema
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        msg = data.get("message", {})
        return ChatResult(
            content=msg.get("content", "") or "",
            tool_calls=msg.get("tool_calls", []) or [],
            prompt_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
        )


async def ollama_available(base_url: str, model: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{base_url}/api/tags")
            r.raise_for_status()
            names = {m.get("name", "") for m in r.json().get("models", [])}
    except httpx.HTTPError as exc:
        return False, f"Ollama not reachable at {base_url} ({type(exc).__name__})"
    if model not in names and f"{model}:latest" not in names:
        return False, f"model '{model}' is not pulled (run: ollama pull {model})"
    return True, "ok"


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0


class ModelGateway:
    """Shared by all agents in one run: enforces the run-wide call budget and retry policy."""

    def __init__(
        self,
        model: ChatModel,
        max_calls: int,
        max_retries: int,
        on_retry: Callable[[str], None] | None = None,
        backoff_s: float = 0.5,
    ) -> None:
        self.model = model
        self.max_calls = max_calls
        self.max_retries = max_retries
        self.on_retry = on_retry or (lambda _msg: None)
        self.backoff_s = backoff_s
        self.usage = Usage()

    async def _call(self, messages: list[dict[str, Any]], **kw: Any) -> ChatResult:
        if self.usage.calls >= self.max_calls:
            raise BudgetExceeded(f"run budget of {self.max_calls} model calls exhausted")
        self.usage.calls += 1
        result = await self.model.chat(messages, **kw)
        self.usage.prompt_tokens += result.prompt_tokens
        self.usage.output_tokens += result.output_tokens
        return result

    async def raw(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResult:
        """One tool-enabled turn. Transport errors are retried; content is interpreted by the caller."""
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._call(messages, tools=tools)
            except (TimeoutError, httpx.HTTPError) as exc:
                last = exc
                self.on_retry(f"model transport error ({type(exc).__name__}), attempt {attempt + 1}")
                await asyncio.sleep(self.backoff_s * (2**attempt))
        raise LLMError(f"model unavailable after {self.max_retries + 1} attempts: {last}")

    async def structured(self, messages: list[dict[str, Any]], out: type[T]) -> T:
        """Ask for JSON matching `out`'s schema; on invalid output, feed the error back and retry."""
        schema = out.model_json_schema()
        convo = list(messages)
        last_err = ""
        for attempt in range(self.max_retries + 1):
            try:
                result = await self._call(convo, schema=schema)
            except (TimeoutError, httpx.HTTPError) as exc:
                last_err = f"transport error: {type(exc).__name__}"
                self.on_retry(f"{last_err}, attempt {attempt + 1}")
                await asyncio.sleep(self.backoff_s * (2**attempt))
                continue
            try:
                return out.model_validate(json.loads(result.content))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_err = str(exc).splitlines()[0][:200]
                self.on_retry(f"invalid structured output ({last_err}), attempt {attempt + 1}")
                convo = [
                    *messages,
                    {"role": "assistant", "content": result.content[:2000]},
                    {
                        "role": "user",
                        "content": f"That was not valid for the schema: {last_err}. "
                        "Reply again with only JSON that matches the schema.",
                    },
                ]
        raise LLMError(f"no valid output after {self.max_retries + 1} attempts ({last_err})")
