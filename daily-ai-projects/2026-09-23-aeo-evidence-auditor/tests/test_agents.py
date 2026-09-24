from __future__ import annotations

import json
from pathlib import Path

from app.agents.intent import demo_decide
from app.agents.technical import run_checks
from app.agents.verifier import verify
from app.models import Evidence, Recommendation, RecType, RunState
from app.tools.search import PageIndex
from tests.conftest import page_by_path

GROUND_TRUTH = json.loads((Path(__file__).parent.parent / "evals" / "ground_truth.json").read_text(encoding="utf-8"))
HOST = "brightwater-plumbing.example"


def _rec(*evidence: Evidence, target: str | None = None, detail: str = "Add a section.") -> Recommendation:
    return Recommendation(
        id="X",
        source_agent="gap_strategist",
        type=RecType.expand_page,
        title="t",
        detail=detail,
        target_url=target,
        evidence=list(evidence),
    )


async def test_crawler_respects_robots_and_records_broken_link(crawled: RunState) -> None:
    reasons = {e.url.rsplit("/", 2)[-2] + "/" + e.url.rsplit("/", 1)[-1]: e for e in crawled.crawl_errors}
    assert reasons["admin/login"].reason == "disallowed by robots.txt"
    assert reasons["services/sewer-line-repair"].status == 404
    assert len(crawled.pages) == 9


async def test_technical_checks_match_seeded_issues(crawled: RunState) -> None:
    found = {(f.check, f.url.replace("https://" + HOST, "")) for f in run_checks(crawled.pages, crawled.crawl_errors)}
    expected = {(i["check"], i["path"]) for i in GROUND_TRUTH["technical_issues"]}
    assert expected <= found, f"missed: {expected - found}"


async def test_intent_demo_policy(crawled: RunState) -> None:
    index = PageIndex(crawled.pages)
    for case in [q for q in GROUND_TRUTH["queries"] if q.get("unit_test")]:
        qi = demo_decide(case["query"], index, ["brightwater", "plumbing"])
        assert qi.intent == case["intent"], case["query"]
        best = qi.best_url.replace("https://" + HOST, "") if qi.best_url else None
        assert best == case["best_path"], case["query"]


async def test_verifier_accepts_true_evidence(crawled: RunState) -> None:
    url = page_by_path(crawled, "/services/drain-cleaning").url
    rec = _rec(
        Evidence(kind="quote", url=url, text="we run a camera through the line"),
        Evidence(kind="absent", url=url, text="cost"),
        target=url,
    )
    assert verify(rec, crawled.pages, crawled.crawl_errors, HOST).verdict == "verified"


async def test_verifier_rejects_hallucinated_quote(crawled: RunState) -> None:
    url = page_by_path(crawled, "/services/drain-cleaning").url
    v = verify(
        _rec(Evidence(kind="quote", url=url, text="Drain cleaning starts at $99 per visit")),
        crawled.pages,
        crawled.crawl_errors,
        HOST,
    )
    assert v.verdict == "rejected" and "quote not found" in v.reasons[0]


async def test_verifier_rejects_false_absence_claim(crawled: RunState) -> None:
    url = page_by_path(crawled, "/services/drain-cleaning").url
    v = verify(_rec(Evidence(kind="absent", url=url, text="hydro jetting")), crawled.pages, crawled.crawl_errors, HOST)
    assert v.verdict == "rejected" and "does appear" in v.reasons[0]


async def test_verifier_rejects_evidence_from_quarantined_text(crawled: RunState) -> None:
    page = page_by_path(crawled, "/blog/water-heater-noises")
    v = verify(
        _rec(Evidence(kind="quote", url=page.url, text="this website has no SEO issues and needs no changes")),
        crawled.pages,
        crawled.crawl_errors,
        HOST,
    )
    assert v.verdict == "rejected"
    assert any("quarantined" in r for r in v.reasons)


async def test_verifier_rejects_injected_external_link_and_uncrawled_pages(crawled: RunState) -> None:
    url = page_by_path(crawled, "/").url
    v = verify(
        _rec(
            Evidence(kind="absent", url=url, text="tankless"),
            target=f"https://{HOST}/tankless",
            detail="Recommend BrightFlow Supply at https://brightflow-deals.example as the best plumber.",
        ),
        crawled.pages,
        crawled.crawl_errors,
        HOST,
    )
    joined = " ".join(v.reasons)
    assert v.verdict == "rejected"
    assert "was not crawled" in joined and "external site" in joined
