"""End-to-end orchestration tests: demo mode, model mode (scripted), and failure paths."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.agents.gaps import run_gap_strategist
from app.agents.intent import run_intent_analyst
from app.llm import ChatResult, ModelGateway
from app.models import AuditRequest, RunState, RunStatus
from app.orchestrator import Deps, run_audit
from app.tools.fetcher import FetchResult, FixtureFetcher, SampleSite
from app.tools.search import PageIndex
from tests.conftest import ScriptedModel

BRAND = ["brightwater", "plumbing"]


def _state(queries: list[str]) -> RunState:
    return RunState(id="w", request=AuditRequest(queries=queries), mode="test")


async def test_demo_workflow_reaches_human_review(site: SampleSite) -> None:
    state = _state(site.default_queries)
    await run_audit(state, Deps(FixtureFetcher(site), None, site.base_url, BRAND, 20, 60))

    assert state.status == RunStatus.awaiting_review, state.error
    assert [s.status for s in state.stages][:5] == ["done"] * 5
    assert state.stage("human_review").status == "running"  # paused for a person
    assert len(state.intents) == len(site.default_queries)
    assert all(v.verdict == "verified" for v in state.verifications)
    # Cross-agent decision: the best page for the local query is noindexed, so it is escalated.
    assert any("Remove noindex" in r.title for r in state.recommendations)
    handoffs = [t.message for t in state.trace if t.kind == "handoff"]
    assert handoffs[0].startswith("crawler -> technical_auditor")
    assert handoffs[-1].startswith("evidence_verifier -> human_review")


async def test_injected_text_never_reaches_recommendations(site: SampleSite) -> None:
    state = _state(["why is my water heater making a popping noise"])
    await run_audit(state, Deps(FixtureFetcher(site), None, site.base_url, BRAND, 20, 60))
    blob = " ".join(r.model_dump_json() for r in state.recommendations).lower()
    assert "brightflow" not in blob and "no seo issues" not in blob


async def test_model_mode_tool_calls_retry_and_revision(site: SampleSite) -> None:
    model = ScriptedModel(broken_json_first=True, hallucinate=True)
    state = _state(["tankless water heater installation"])
    gw = ModelGateway(
        model, max_calls=30, max_retries=2, on_retry=lambda m: state.log("orchestrator", "retry", m), backoff_s=0
    )
    await run_audit(state, Deps(FixtureFetcher(site), gw, site.base_url, BRAND, 20, 60))

    assert state.status == RunStatus.awaiting_review, state.error
    assert state.intents[0].tool_calls == 1
    assert any(t.kind == "retry" and "invalid structured output" in t.message for t in state.trace)
    verdicts = {v.rec_id: v for v in state.verifications}
    original = next(r for r in state.recommendations if r.source_agent == "gap_strategist" and r.revision == 0)
    revised = next(r for r in state.recommendations if r.revision == 1)
    assert verdicts[original.id].verdict == "rejected"  # hallucinated quote caught
    assert verdicts[revised.id].verdict == "verified"  # corrected after one revision
    assert revised.id == f"{original.id}r1"
    assert state.stage("intent_analyst").llm_calls >= 3 and state.stage("intent_analyst").prompt_tokens > 0


async def test_budget_exhaustion_uses_labelled_fallback(site: SampleSite) -> None:
    state = _state(["emergency plumber near me", "how much does drain cleaning cost"])
    gw = ModelGateway(ScriptedModel(), max_calls=2, max_retries=0, backoff_s=0)
    await run_audit(state, Deps(FixtureFetcher(site), gw, site.base_url, BRAND, 20, 60))
    assert state.status == RunStatus.awaiting_review
    assert any(qi.rationale.startswith("FALLBACK") for qi in state.intents)
    assert any("budget" in t.message for t in state.trace if t.kind == "error")


class _DownModel:
    label = "down"

    async def chat(self, messages: list[dict[str, Any]], schema: Any = None, tools: Any = None) -> ChatResult:
        raise httpx.ConnectError("connection refused")


async def test_model_outage_is_retried_then_degrades(site: SampleSite) -> None:
    state = _state(["emergency plumber near me"])
    gw = ModelGateway(
        _DownModel(), max_calls=20, max_retries=2, on_retry=lambda m: state.log("orchestrator", "retry", m), backoff_s=0
    )
    await run_audit(state, Deps(FixtureFetcher(site), gw, site.base_url, BRAND, 20, 60))
    assert state.status == RunStatus.awaiting_review
    assert sum(1 for t in state.trace if t.kind == "retry") == 3  # 1 attempt + 2 retries
    assert state.intents[0].rationale.startswith("FALLBACK")


async def test_unreachable_site_fails_cleanly(site: SampleSite) -> None:
    state = _state(["x"])
    fetcher = FixtureFetcher(site, fail_paths={"/"})
    await run_audit(state, Deps(fetcher, None, site.base_url, BRAND, 20, 60))
    assert state.status == RunStatus.failed
    assert state.error == "no pages could be crawled; nothing to audit"
    assert state.stage("crawler").status == "failed"
    assert any(t.kind == "retry" for t in state.trace)  # the crawler retried once before giving up


async def test_page_limit_is_a_stop_condition(site: SampleSite) -> None:
    state = _state(["drain cleaning"])
    await run_audit(state, Deps(FixtureFetcher(site), None, site.base_url, BRAND, 3, 60))
    assert len(state.pages) == 3
    assert any("page limit (3) reached" in t.message for t in state.trace)


class _FlakyFetcher(FixtureFetcher):
    def __init__(self, site: SampleSite) -> None:
        super().__init__(site)
        self.failed_once = False

    async def fetch(self, url: str) -> FetchResult:
        if url.endswith("/about") and not self.failed_once:
            self.failed_once = True
            return FetchResult(url, 0, "", error="timed out")
        return await super().fetch(url)


async def test_transient_fetch_error_is_retried(site: SampleSite) -> None:
    state = _state(["about"])
    await run_audit(state, Deps(_FlakyFetcher(site), None, site.base_url, BRAND, 20, 60))
    assert any(p.url.endswith("/about") for p in state.pages)


async def test_deadline_stops_new_model_calls(crawled: RunState) -> None:
    model = ScriptedModel(hallucinate=False)
    gw = ModelGateway(model, max_calls=30, max_retries=0, backoff_s=0)
    index = PageIndex(crawled.pages)
    await run_intent_analyst(crawled, index, gw, BRAND, deadline=time.monotonic() - 1)
    recs = await run_gap_strategist(crawled, index, gw, deadline=time.monotonic() - 1)
    assert model.calls == []  # deadline already passed: no model call is made
    assert all(qi.rationale.startswith("FALLBACK (deadline)") for qi in crawled.intents)
    assert recs == []


async def test_unfounded_strong_match_is_downgraded(crawled: RunState) -> None:
    from app.models import QueryIntent

    url = next(p.url for p in crawled.pages if p.url.endswith("/services/water-heater-repair"))
    crawled.intents = [
        QueryIntent(
            query="tankless water heater installation",
            intent="transactional",
            best_url=url,
            match="strong",
            rationale="model says the page offers tankless installs",
        )
    ]
    recs = await run_gap_strategist(crawled, PageIndex(crawled.pages), None)
    assert crawled.intents[0].match == "partial"
    assert crawled.intents[0].rationale.startswith("[Downgraded to partial")
    assert any(e.kind == "absent" and e.text == "tankless" for r in recs for e in r.evidence)
