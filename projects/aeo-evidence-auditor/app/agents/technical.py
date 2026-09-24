"""Technical Auditor agent.

Responsibility: rule-based technical SEO checks on every crawled page.
Tools:          the check functions below (pure functions over PageSnapshots).  No model: these
                checks have exact answers, so an LLM would add cost and error without adding value.
Input:          pages + crawl errors from the Crawler.
Output:         TechnicalFindings (each with field-level evidence) and grouped fix_technical
                Recommendations for the verifier.
Handoff:        always -> Intent Analyst (technical findings do not block content analysis).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

from app.models import (
    CrawlError,
    Evidence,
    PageSnapshot,
    Recommendation,
    RecType,
    RunState,
    Severity,
    TechnicalFinding,
)

THIN_CONTENT_WORDS = 250
TITLE_MAX = 60
META_MAX = 160

CHECK_ADVICE: dict[str, tuple[str, str]] = {
    "missing_title": (
        "Add a unique <title> to {n} page(s)",
        "Search engines and answer engines use the title as the primary label for a page.",
    ),
    "long_title": (
        "Shorten {n} title(s) to about 60 characters",
        "Long titles are truncated in results, hiding the part that tells a searcher what the page covers.",
    ),
    "duplicate_title": (
        "Make {n} duplicated title(s) unique",
        "Pages sharing a title compete with each other and give engines no signal about which answers which query.",
    ),
    "missing_meta_description": (
        "Write meta descriptions for {n} page(s)",
        "Without one, engines pick an arbitrary snippet. A written summary improves click-through and is often reused by AI answers.",
    ),
    "long_meta_description": (
        "Trim {n} meta description(s) to about 160 characters",
        "Descriptions beyond ~160 characters are cut off in results.",
    ),
    "missing_h1": (
        "Add a single descriptive H1 to {n} page(s)",
        "The H1 states the page topic for readers, screen readers and crawlers.",
    ),
    "multiple_h1": ("Reduce to one H1 on {n} page(s)", "Several H1s blur the page's primary topic."),
    "noindex": (
        "Review noindex on {n} page(s)",
        "These pages ask search engines not to index them, so they cannot rank or be cited. Confirm this is intentional.",
    ),
    "missing_canonical": (
        "Add canonical tags to {n} page(s)",
        "A self-referencing canonical prevents duplicate URL variants from splitting signals.",
    ),
    "images_missing_alt": (
        "Add alt text to images on {n} page(s)",
        "Alt text is required for accessibility (WCAG 1.1.1) and helps image search.",
    ),
    "broken_internal_link": (
        "Fix {n} broken internal link(s)",
        "Links to missing pages waste crawl budget and frustrate visitors.",
    ),
    "thin_content": (
        "Expand {n} thin page(s)",
        f"Pages under {THIN_CONTENT_WORDS} words rarely answer a query fully enough to rank or be quoted.",
    ),
    "missing_structured_data": (
        "Add structured data to {n} page(s)",
        "JSON-LD (e.g. LocalBusiness, Service, FAQPage) lets engines understand entities and eligible rich results.",
    ),
    "invalid_structured_data": (
        "Fix invalid JSON-LD on {n} page(s)",
        "Malformed JSON-LD is ignored entirely by search engines.",
    ),
}

SEVERITY = {
    "missing_title": Severity.high,
    "noindex": Severity.high,
    "broken_internal_link": Severity.high,
    "missing_h1": Severity.high,
    "duplicate_title": Severity.medium,
    "missing_meta_description": Severity.medium,
    "multiple_h1": Severity.medium,
    "images_missing_alt": Severity.medium,
    "thin_content": Severity.medium,
    "invalid_structured_data": Severity.medium,
    "long_title": Severity.low,
    "long_meta_description": Severity.low,
    "missing_canonical": Severity.low,
    "missing_structured_data": Severity.low,
}


def _f(check: str, url: str, message: str, *evidence: Evidence) -> tuple[str, str, str, list[Evidence]]:
    return check, url, message, list(evidence)


def _page_checks(p: PageSnapshot) -> Iterator[tuple[str, str, str, list[Evidence]]]:
    u = p.url
    if not p.title:
        yield _f(
            "missing_title", u, "Page has no <title>.", Evidence(kind="field", url=u, field="title", text="(missing)")
        )
    elif len(p.title) > TITLE_MAX:
        yield _f(
            "long_title",
            u,
            f"Title is {len(p.title)} characters.",
            Evidence(kind="field", url=u, field="title", text=p.title),
        )
    if not p.meta_description:
        yield _f(
            "missing_meta_description",
            u,
            "No meta description.",
            Evidence(kind="field", url=u, field="meta_description", text="(missing)"),
        )
    elif len(p.meta_description) > META_MAX:
        yield _f(
            "long_meta_description",
            u,
            f"Meta description is {len(p.meta_description)} characters.",
            Evidence(kind="field", url=u, field="meta_description", text=p.meta_description[:300]),
        )
    if not p.h1:
        yield _f("missing_h1", u, "No H1 heading.", Evidence(kind="field", url=u, field="h1", text="(missing)"))
    elif len(p.h1) > 1:
        yield _f(
            "multiple_h1",
            u,
            f"{len(p.h1)} H1 headings.",
            *[Evidence(kind="field", url=u, field="h1", text=h[:300]) for h in p.h1[:4]],
        )
    if p.robots_meta and "noindex" in p.robots_meta.lower():
        yield _f(
            "noindex",
            u,
            f"robots meta is '{p.robots_meta}'.",
            Evidence(kind="field", url=u, field="robots_meta", text=p.robots_meta),
        )
    if not p.canonical:
        yield _f(
            "missing_canonical",
            u,
            "No canonical link.",
            Evidence(kind="field", url=u, field="canonical", text="(missing)"),
        )
    if p.images_missing_alt:
        yield _f(
            "images_missing_alt",
            u,
            f"{len(p.images_missing_alt)} image(s) without alt text.",
            *[Evidence(kind="field", url=u, field="images_missing_alt", text=src) for src in p.images_missing_alt[:4]],
        )
    if p.word_count < THIN_CONTENT_WORDS:
        yield _f(
            "thin_content",
            u,
            f"Only {p.word_count} words of visible copy.",
            Evidence(kind="field", url=u, field="word_count", text=str(p.word_count)),
        )
    if "InvalidJSON-LD" in p.jsonld_types:
        yield _f(
            "invalid_structured_data",
            u,
            "A JSON-LD block could not be parsed.",
            Evidence(kind="field", url=u, field="jsonld_types", text="InvalidJSON-LD"),
        )
    elif not p.jsonld_types:
        yield _f(
            "missing_structured_data",
            u,
            "No JSON-LD structured data.",
            Evidence(kind="field", url=u, field="jsonld_types", text="(missing)"),
        )


def run_checks(pages: list[PageSnapshot], errors: list[CrawlError]) -> list[TechnicalFinding]:
    raw: list[tuple[str, str, str, list[Evidence]]] = []
    for p in pages:
        raw.extend(_page_checks(p))

    by_title: dict[str, list[PageSnapshot]] = defaultdict(list)
    for p in pages:
        if p.title:
            by_title[p.title.strip().lower()].append(p)
    for group in by_title.values():
        if len(group) > 1:
            for p in group:
                others = ", ".join(g.url for g in group if g is not p)
                raw.append(
                    _f(
                        "duplicate_title",
                        p.url,
                        f"Title also used by {others}.",
                        Evidence(kind="field", url=p.url, field="title", text=p.title or ""),
                    )
                )

    for e in errors:
        if e.status == 404 and e.found_on:
            raw.append(
                _f(
                    "broken_internal_link",
                    e.found_on,
                    f"Links to {e.url}, which returns 404.",
                    Evidence(kind="field", url=e.found_on, field="internal_links", text=e.url),
                    Evidence(kind="field", url=e.url, field="status", text="404"),
                )
            )

    return [
        TechnicalFinding(id=f"T{i:03d}", check=c, severity=SEVERITY[c], url=u, message=m, evidence=ev)
        for i, (c, u, m, ev) in enumerate(raw, start=1)
    ]


_ORDER = {Severity.high: 0, Severity.medium: 1, Severity.low: 2}


def findings_to_recommendations(findings: list[TechnicalFinding]) -> list[Recommendation]:
    grouped: dict[str, list[TechnicalFinding]] = defaultdict(list)
    for f in findings:
        grouped[f.check].append(f)
    recs: list[Recommendation] = []
    for check, items in sorted(grouped.items(), key=lambda kv: (_ORDER[SEVERITY[kv[0]]], kv[0])):
        title_tpl, why = CHECK_ADVICE[check]
        urls = sorted({f.url for f in items})
        evidence = [ev for f in items for ev in f.evidence][:6]
        pages = ", ".join(urls[:5]) + (f" and {len(urls) - 5} more" if len(urls) > 5 else "")
        recs.append(
            Recommendation(
                id=f"R-{check}",
                source_agent="technical_auditor",
                type=RecType.add_schema if "structured_data" in check else RecType.fix_technical,
                target_url=urls[0] if len(urls) == 1 else None,
                title=title_tpl.format(n=len(items)),
                detail=f"{why} Affected: {pages}."[:700],
                priority=SEVERITY[check],
                evidence=evidence,
            )
        )
    return recs


def run_technical_auditor(state: RunState) -> None:
    state.findings = run_checks(state.pages, state.crawl_errors)
    recs = findings_to_recommendations(state.findings)
    state.recommendations.extend(recs)
    counts = {s: sum(1 for f in state.findings if f.severity == s) for s in Severity}
    state.log(
        "technical_auditor",
        "tool",
        f"{len(state.findings)} findings (high {counts[Severity.high]}, medium {counts[Severity.medium]}, "
        f"low {counts[Severity.low]}) grouped into {len(recs)} recommendations",
    )
