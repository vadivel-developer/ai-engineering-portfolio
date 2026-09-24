from __future__ import annotations

import json
import time
from typing import Any

import pytest

from app.agents.crawler import run_crawler
from app.llm import ChatResult
from app.models import AuditRequest, PageSnapshot, RunState
from app.tools.fetcher import FixtureFetcher, SampleSite, get_sample


@pytest.fixture
def site() -> SampleSite:
    return get_sample("brightwater-plumbing")


@pytest.fixture
async def crawled(site: SampleSite) -> RunState:
    state = RunState(id="t", request=AuditRequest(queries=site.default_queries), mode="test")
    await run_crawler(state, FixtureFetcher(site), site.base_url, 20, time.monotonic() + 30)
    return state


def page_by_path(state: RunState, path: str) -> PageSnapshot:
    for p in state.pages:
        if p.url.endswith(path):
            return p
    raise KeyError(path)


class ScriptedModel:
    """Stands in for a local LLM so the model-driven code paths are tested deterministically.

    - Tool turns: calls search_pages once, then stops calling tools.
    - IntentDecision: answers from the last search result. Optionally returns broken JSON first.
    - GapPlan: first answer contains a hallucinated quote; the revision answer is correct.
    """

    label = "scripted test model"

    def __init__(self, broken_json_first: bool = False, hallucinate: bool = True) -> None:
        self.broken_json_first = broken_json_first
        self.hallucinate = hallucinate
        self.calls: list[str] = []
        self._broke = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        if tools:
            self.calls.append("tools")
            if messages[-1]["role"] == "tool":
                return ChatResult(content="", prompt_tokens=50, output_tokens=5)
            query = json.loads(messages[1]["content"].split(":", 1)[1])
            return ChatResult(
                content="",
                tool_calls=[{"function": {"name": "search_pages", "arguments": {"query": query}}}],
                prompt_tokens=40,
                output_tokens=12,
            )

        title = (schema or {}).get("title")
        self.calls.append(str(title))
        if title == "IntentDecision":
            if self.broken_json_first and not self._broke:
                self._broke = True
                return ChatResult(content='{"intent": "informational", ', prompt_tokens=60, output_tokens=8)
            tool_results = [m for m in messages if m["role"] == "tool" or "search_pages result" in m.get("content", "")]
            raw = tool_results[-1]["content"]
            hits = json.loads(raw[raw.index("[") :])
            best = hits[0]["url"] if hits else None
            return ChatResult(
                content=json.dumps(
                    {
                        "intent": "informational",
                        "best_url": best,
                        "match": "partial" if best else "none",
                        "rationale": "scripted",
                    }
                ),
                prompt_tokens=80,
                output_tokens=30,
            )
        if title == "GapPlan":
            prompt = messages[1]["content"]
            url = prompt.split("<<<UNTRUSTED_PAGE_CONTENT ", 1)[1].split(">>>", 1)[0]
            revising = any("rejected this recommendation" in m.get("content", "") for m in messages)
            text = "We install tankless water heaters every week" if self.hallucinate and not revising else "zzzqqq"
            kind = "quote" if self.hallucinate and not revising else "absent"
            plan = {
                "recommendations": [
                    {
                        "type": "expand_page",
                        "title": "Add an answer section",
                        "detail": "Add it.",
                        "priority": "medium",
                        "evidence": [{"kind": kind, "url": url, "text": text}],
                    }
                ]
            }
            return ChatResult(content=json.dumps(plan), prompt_tokens=400, output_tokens=60)
        raise AssertionError(f"unexpected schema {title}")
