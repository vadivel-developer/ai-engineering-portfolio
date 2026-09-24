"""Orchestrator: a bounded state machine over the agents. It owns shared state, timing, the deadline,
the model-call budget, the verify -> revise loop, and the pause for human review.

    crawler ─► technical_auditor ─► intent_analyst ─► gap_strategist ─► evidence_verifier ─► human_review
                                                            ▲                    │
                                                            └── 1 revision ◄─────┘ (rejected items only)
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

from app.agents.crawler import run_crawler
from app.agents.gaps import demo_revise, llm_revise, page_for, run_gap_strategist
from app.agents.intent import run_intent_analyst
from app.agents.technical import run_technical_auditor
from app.agents.verifier import check_evidence, verify
from app.llm import LLMError, ModelGateway
from app.models import AgentName, Recommendation, RunState, RunStatus, StageMetrics, Verification
from app.tools.fetcher import Fetcher
from app.tools.search import PageIndex

log = logging.getLogger("aeo.orchestrator")

PIPELINE: list[AgentName] = [
    "crawler",
    "technical_auditor",
    "intent_analyst",
    "gap_strategist",
    "evidence_verifier",
    "human_review",
]
MAX_REVISIONS = 1


@dataclass
class Deps:
    fetcher: Fetcher
    gateway: ModelGateway | None  # None = deterministic demonstration mode
    start_url: str
    brand_terms: list[str]  # brand name words, most distinctive first
    max_pages: int
    deadline_s: int


class StageFailed(RuntimeError):
    pass


def init_stages(state: RunState) -> None:
    state.stages = [StageMetrics(agent=a) for a in PIPELINE]


@asynccontextmanager
async def stage(state: RunState, agent: AgentName, gw: ModelGateway | None) -> AsyncIterator[StageMetrics]:
    m = state.stage(agent)
    m.status, m.started = "running", time.time()
    before = (gw.usage.calls, gw.usage.prompt_tokens, gw.usage.output_tokens) if gw else (0, 0, 0)
    state.log(agent, "start", f"{agent.replace('_', ' ')} started")
    try:
        yield m
    except Exception:
        m.status = "failed"
        raise
    else:
        m.status = "done"
    finally:
        m.ended = time.time()
        if gw:
            m.llm_calls = gw.usage.calls - before[0]
            m.prompt_tokens = gw.usage.prompt_tokens - before[1]
            m.output_tokens = gw.usage.output_tokens - before[2]


def _skip(state: RunState, agent: AgentName, why: str) -> None:
    state.stage(agent).status = "skipped"
    state.log("orchestrator", "decision", f"skipping {agent}: {why}")


def _assign_ids(recs: list[Recommendation], prefix: str, start: int) -> None:
    for i, r in enumerate(recs, start=start):
        r.id = f"{prefix}{i:02d}"


async def run_audit(state: RunState, deps: Deps) -> None:
    gw = deps.gateway
    deadline = time.monotonic() + deps.deadline_s
    site_host = urlparse(deps.start_url).netloc.lower()
    state.status = RunStatus.running
    init_stages(state)
    state.log(
        "orchestrator", "start", f"audit of {deps.start_url} · {len(state.request.queries)} queries · {state.mode}"
    )

    try:
        # 1. Crawl ---------------------------------------------------------------------------
        async with stage(state, "crawler", gw):
            await run_crawler(state, deps.fetcher, deps.start_url, deps.max_pages, deadline)
            if not state.pages:
                raise StageFailed("no pages could be crawled; nothing to audit")
        state.log("orchestrator", "handoff", f"crawler -> technical_auditor ({len(state.pages)} pages)")

        # 2. Technical checks (deterministic) ------------------------------------------------
        async with stage(state, "technical_auditor", gw):
            run_technical_auditor(state)
        state.log("orchestrator", "handoff", f"technical_auditor -> intent_analyst ({len(state.findings)} findings)")

        index = PageIndex(state.pages)
        out_of_time = time.monotonic() > deadline

        # 3. Intent ------------------------------------------------------------------------
        if out_of_time:
            _skip(state, "intent_analyst", "run deadline reached")
        else:
            async with stage(state, "intent_analyst", gw):
                await run_intent_analyst(state, index, gw, deps.brand_terms, deadline)
            state.log("orchestrator", "handoff", f"intent_analyst -> gap_strategist ({len(state.intents)} intents)")

        # 4. Gaps --------------------------------------------------------------------------
        gap_recs: list[Recommendation] = []
        if not state.intents or time.monotonic() > deadline:
            _skip(state, "gap_strategist", "no intents" if not state.intents else "run deadline reached")
        else:
            async with stage(state, "gap_strategist", gw):
                gap_recs = await run_gap_strategist(state, index, gw, deadline)
                _assign_ids(gap_recs, "G", 1)
                state.recommendations.extend(gap_recs)
            state.log("orchestrator", "handoff", f"gap_strategist -> evidence_verifier ({len(gap_recs)} recs)")

        # 5. Verify, with one bounded revision round for rejected strategist output ---------
        async with stage(state, "evidence_verifier", gw):
            await _verify_and_revise(state, index, gw, site_host)

        state.status = RunStatus.awaiting_review
        state.stage("human_review").status = "running"
        state.stage("human_review").started = time.time()
        verified = sum(1 for v in state.verifications if v.verdict == "verified")
        state.log(
            "orchestrator",
            "handoff",
            f"evidence_verifier -> human_review ({verified} verified, "
            f"{len(state.verifications) - verified} rejected). Waiting for a reviewer.",
        )

    except StageFailed as exc:
        state.status, state.error = RunStatus.failed, str(exc)
        state.log("orchestrator", "error", str(exc))
    except Exception as exc:  # never leave a run stuck in "running"
        log.exception("run %s crashed", state.id)
        state.status, state.error = RunStatus.failed, f"internal error: {type(exc).__name__}"
        state.log("orchestrator", "error", state.error)
    finally:
        await deps.fetcher.aclose()


async def _verify_and_revise(state: RunState, index: PageIndex, gw: ModelGateway | None, site_host: str) -> None:
    pages = {p.url: p for p in state.pages}
    errors = {e.url: e for e in state.crawl_errors}
    results: dict[str, Verification] = {}
    for rec in state.recommendations:
        results[rec.id] = verify(rec, state.pages, state.crawl_errors, site_host)

    intents = {qi.query: qi for qi in state.intents}
    revised: list[Recommendation] = []
    for rec in list(state.recommendations):
        v = results[rec.id]
        if v.verdict == "verified" or rec.source_agent != "gap_strategist" or rec.revision >= MAX_REVISIONS:
            continue
        state.log("evidence_verifier", "decision", f"{rec.id} rejected: {'; '.join(v.reasons)[:300]}")
        state.log("orchestrator", "handoff", f"evidence_verifier -> gap_strategist: revise {rec.id} (1 attempt)")
        qi = intents.get(rec.query or "")
        page = page_for(qi, index, pages) if qi else None
        try:
            if gw is None or qi is None or page is None:
                failing = {i for i, e in enumerate(rec.evidence) if check_evidence(e, pages, errors)}
                new = demo_revise(rec, failing)
            else:
                new = await llm_revise(rec, v.reasons, qi, page, gw)
        except LLMError as exc:
            state.log("gap_strategist", "error", f"revision of {rec.id} failed: {exc}")
            new = []
        if not new:
            state.log("gap_strategist", "decision", f"{rec.id} withdrawn: evidence could not be supported")
        for n in new:
            n.id = f"{rec.id}r{n.revision}"
            revised.append(n)

    for n in revised:
        state.recommendations.append(n)
        results[n.id] = verify(n, state.pages, state.crawl_errors, site_host)
        state.log("evidence_verifier", "decision", f"{n.id} (revision) {results[n.id].verdict}")

    state.verifications = [results[r.id] for r in state.recommendations]
    ok = sum(1 for v in state.verifications if v.verdict == "verified")
    state.log("evidence_verifier", "done", f"{ok}/{len(state.verifications)} recommendations verified")
