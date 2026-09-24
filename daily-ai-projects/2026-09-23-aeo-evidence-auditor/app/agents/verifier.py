"""Evidence Verifier agent.

Responsibility: independently check every recommendation against the crawled pages before a human
                sees it.  It never trusts the agent that wrote the recommendation.
Tools:          exact-match evidence checks over shared state.  No model: verification must not share
                the failure modes (hallucination, injection) of the model it is checking.
Input:          Recommendations + pages + crawl errors.
Output:         one Verification (verified / rejected + reasons) per recommendation.
Handoff:        rejected Gap Strategist recommendations -> back to the Gap Strategist for ONE revision;
                everything else -> human review.  Rejected items are shown to the reviewer, never
                approvable.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.models import CrawlError, Evidence, PageSnapshot, Recommendation, Verification
from app.safety import looks_like_injection, norm_text

MIN_QUOTE_CHARS = 12
_URL_RE = re.compile(r"https?://[^\s)\"']+", re.IGNORECASE)

LIST_FIELDS = {"h1", "headings", "jsonld_types", "images_missing_alt", "internal_links"}
SCALAR_FIELDS = {"title", "meta_description", "canonical", "robots_meta", "word_count", "status"}


def page_haystack(p: PageSnapshot) -> str:
    return norm_text(" \n ".join([p.title or "", p.meta_description or "", *p.h1, *p.headings, p.text]))


def field_values(p: PageSnapshot, field: str) -> list[str]:
    value = getattr(p, field)
    if isinstance(value, list):
        return [str(v) for v in value]
    if value is None or value == "":
        return []
    return [str(value)]


def check_evidence(ev: Evidence, pages: dict[str, PageSnapshot], errors: dict[str, CrawlError]) -> str | None:
    """Return None when the evidence holds, otherwise the reason it fails."""
    if ev.kind == "field" and ev.field == "status":
        err = errors.get(ev.url)
        observed = str(err.status) if err and err.status else ("200" if ev.url in pages else None)
        return None if observed == ev.text else f"status of {ev.url} is {observed}, not {ev.text}"

    page = pages.get(ev.url)
    if page is None:
        return f"evidence cites {ev.url}, which was not crawled"

    if ev.kind == "quote":
        q = norm_text(ev.text)
        if len(q) < MIN_QUOTE_CHARS:
            return f"quote too short to verify: '{ev.text}'"
        if any(q in norm_text(line) or norm_text(line) in q for line in page.quarantined):
            return "quote comes from quarantined text (suspected prompt injection)"
        return None if q in page_haystack(page) else f"quote not found on {ev.url}: '{ev.text[:80]}'"

    if ev.kind == "absent":
        term = norm_text(ev.text)
        if not 3 <= len(term) <= 60:
            return f"absence term must be 3-60 characters: '{ev.text}'"
        return f"'{ev.text}' does appear on {ev.url}" if term in page_haystack(page) else None

    # kind == "field"
    if ev.field not in LIST_FIELDS | SCALAR_FIELDS:
        return f"unknown field '{ev.field}'"
    values = field_values(page, ev.field)
    if ev.text == "(missing)":
        return None if not values else f"{ev.field} is present on {ev.url}"
    return (
        None
        if norm_text(ev.text) in {norm_text(v) for v in values}
        else (f"{ev.field} on {ev.url} does not contain '{ev.text[:80]}'")
    )


def verify(rec: Recommendation, pages: list[PageSnapshot], errors: list[CrawlError], site_host: str) -> Verification:
    by_url = {p.url: p for p in pages}
    err_by_url = {e.url: e for e in errors}
    reasons: list[str] = []

    if rec.target_url is not None and rec.target_url not in by_url:
        reasons.append(f"target page {rec.target_url} was not crawled")
    for ev in rec.evidence:
        reason = check_evidence(ev, by_url, err_by_url)
        if reason:
            reasons.append(reason)
    prose = f"{rec.title}\n{rec.detail}"
    if looks_like_injection(prose):
        reasons.append("recommendation text contains instruction-like content")
    for link in _URL_RE.findall(prose):
        if urlparse(link).netloc.lower() != site_host:
            reasons.append(f"recommendation links to an external site: {link}")
    if rec.type.value in {"expand_page", "add_faq", "new_page"} and not any(
        e.kind in {"quote", "absent"} for e in rec.evidence
    ):
        reasons.append("content recommendation has no quote or absence evidence")

    return Verification(rec_id=rec.id, verdict="rejected" if reasons else "verified", reasons=reasons)
