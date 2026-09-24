"""Typed contracts shared by every agent. Agents only communicate through these structures."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------- inputs


class AuditRequest(BaseModel):
    source: Literal["sample", "live"] = "sample"
    sample_id: str = Field(default="brightwater-plumbing", pattern=r"^[a-z0-9-]{1,60}$")
    start_url: str | None = Field(default=None, max_length=500)
    queries: list[str] = Field(min_length=1, max_length=12)
    max_pages: int = Field(default=15, ge=1, le=50)

    @field_validator("queries")
    @classmethod
    def _clean_queries(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for q in value:
            q = " ".join(q.split())
            if not q:
                continue
            if len(q) > 120:
                raise ValueError("each query must be 120 characters or fewer")
            if q.lower() not in {c.lower() for c in cleaned}:
                cleaned.append(q)
        if not cleaned:
            raise ValueError("at least one non-empty query is required")
        return cleaned


# ---------------------------------------------------------------- crawl output


class PageSnapshot(BaseModel):
    url: str
    status: int
    title: str | None = None
    meta_description: str | None = None
    canonical: str | None = None
    robots_meta: str | None = None
    h1: list[str] = Field(default_factory=list)
    headings: list[str] = Field(default_factory=list)  # h2/h3 text, in order
    jsonld_types: list[str] = Field(default_factory=list)
    images_missing_alt: list[str] = Field(default_factory=list)
    internal_links: list[str] = Field(default_factory=list)
    word_count: int = 0
    text: str = ""  # visible text with suspected injected instructions removed
    quarantined: list[str] = Field(default_factory=list)  # lines removed as suspected prompt injection


class CrawlError(BaseModel):
    url: str
    reason: str
    status: int | None = None
    found_on: str | None = None  # page that linked to it (for broken-link reporting)


# ---------------------------------------------------------------- findings / recommendations


class Evidence(BaseModel):
    """A claim about a crawled page that the verifier can check mechanically.

    kind="quote":   `text` must appear verbatim (whitespace-normalised) in the page.
    kind="absent":  `text` must NOT appear anywhere in the page (a content gap).
    kind="field":   `field` of the page snapshot has the observed `text` value.
    """

    kind: Literal["quote", "absent", "field"]
    url: str
    text: str = Field(max_length=300)
    field: str | None = None


class Severity(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class TechnicalFinding(BaseModel):
    id: str
    check: str
    severity: Severity
    url: str
    message: str
    evidence: list[Evidence]


Intent = Literal["informational", "commercial", "transactional", "navigational", "local"]


class QueryIntent(BaseModel):
    query: str
    intent: Intent
    best_url: str | None
    match: Literal["strong", "partial", "none"]
    rationale: str = Field(max_length=400)
    tool_calls: int = 0


class RecType(StrEnum):
    fix_technical = "fix_technical"
    expand_page = "expand_page"
    add_faq = "add_faq"
    add_schema = "add_schema"
    new_page = "new_page"


class Recommendation(BaseModel):
    id: str
    source_agent: Literal["technical_auditor", "gap_strategist"]
    type: RecType
    query: str | None = None
    target_url: str | None = None
    title: str = Field(max_length=140)
    detail: str = Field(max_length=700)
    priority: Severity = Severity.medium
    evidence: list[Evidence] = Field(min_length=1, max_length=6)
    revision: int = 0


class Verification(BaseModel):
    rec_id: str
    verdict: Literal["verified", "rejected"]
    reasons: list[str] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    rec_id: str
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=500)


class ReviewSubmission(BaseModel):
    reviewer: str = Field(min_length=1, max_length=80)
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------- run state / tracing


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    awaiting_review = "awaiting_review"
    completed = "completed"
    failed = "failed"


AgentName = Literal[
    "orchestrator",
    "crawler",
    "technical_auditor",
    "intent_analyst",
    "gap_strategist",
    "evidence_verifier",
    "human_review",
]


TraceKind = Literal["start", "tool", "handoff", "retry", "warning", "error", "done", "decision"]


class TraceEvent(BaseModel):
    ts: float = Field(default_factory=time.time)
    agent: AgentName
    kind: TraceKind
    message: str


class StageMetrics(BaseModel):
    agent: AgentName
    status: Literal["pending", "running", "done", "skipped", "failed"] = "pending"
    started: float | None = None
    ended: float | None = None
    llm_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0

    @property
    def elapsed_ms(self) -> int | None:
        if self.started is None or self.ended is None:
            return None
        return int((self.ended - self.started) * 1000)


class RunState(BaseModel):
    id: str
    created: float = Field(default_factory=time.time)
    request: AuditRequest
    mode: str  # human-readable backend label shown in the UI
    site_url: str = ""
    status: RunStatus = RunStatus.queued
    error: str | None = None
    pages: list[PageSnapshot] = Field(default_factory=list)
    crawl_errors: list[CrawlError] = Field(default_factory=list)
    findings: list[TechnicalFinding] = Field(default_factory=list)
    intents: list[QueryIntent] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    review: list[ReviewDecision] = Field(default_factory=list)
    reviewer: str | None = None
    trace: list[TraceEvent] = Field(default_factory=list)
    stages: list[StageMetrics] = Field(default_factory=list)

    def log(self, agent: AgentName, kind: TraceKind, message: str) -> None:
        self.trace.append(TraceEvent(agent=agent, kind=kind, message=message[:500]))

    def stage(self, agent: AgentName) -> StageMetrics:
        for s in self.stages:
            if s.agent == agent:
                return s
        raise KeyError(agent)
