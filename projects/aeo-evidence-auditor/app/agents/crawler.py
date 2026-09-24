"""Crawler agent.

Responsibility: collect a bounded, same-site set of pages.
Tools:          fetcher.allowed (robots.txt), fetcher.fetch, extract_page.  No model, no write access.
Input:          start URL, page limit, deadline.
Output:         PageSnapshots + CrawlErrors in shared state.
Decisions:      which discovered link to visit next (breadth-first, same host only), whether to retry a
                failed fetch (once, network errors only), and when to stop (frontier empty, page limit,
                deadline).
Handoff:        >= 1 page crawled -> Technical Auditor.  0 pages -> orchestrator fails the run.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from app.models import CrawlError, RunState
from app.safety import looks_like_asset, normalize_url, same_site
from app.tools.fetcher import Fetcher
from app.tools.html_extract import extract_page


async def run_crawler(state: RunState, fetcher: Fetcher, start_url: str, max_pages: int, deadline: float) -> str:
    start = normalize_url(start_url)
    frontier: deque[tuple[str, str | None]] = deque([(start, None)])
    seen = {start}
    stop_reason = "frontier exhausted"

    while frontier:
        if len(state.pages) >= max_pages:
            stop_reason = f"page limit ({max_pages}) reached"
            break
        if time.monotonic() > deadline:
            stop_reason = "run deadline reached"
            break
        url, found_on = frontier.popleft()

        if not await fetcher.allowed(url):
            state.crawl_errors.append(CrawlError(url=url, reason="disallowed by robots.txt", found_on=found_on))
            state.log("crawler", "decision", f"skip {url}: disallowed by robots.txt")
            continue

        result = await fetcher.fetch(url)
        if result.status == 0 and result.error and not result.error.startswith("blocked"):
            state.log("crawler", "retry", f"{url}: {result.error}; retrying once")
            await asyncio.sleep(0.2)
            result = await fetcher.fetch(url)

        if result.status != 200 or not result.html:
            state.crawl_errors.append(
                CrawlError(
                    url=url,
                    reason=result.error or f"HTTP {result.status}",
                    status=result.status or None,
                    found_on=found_on,
                )
            )
            state.log("crawler", "warning", f"{url} -> {result.error or result.status}")
            continue

        page = extract_page(result.url, result.status, result.html)
        state.pages.append(page)
        state.log("crawler", "tool", f"fetched {page.url} ({page.word_count} words, {len(page.internal_links)} links)")
        if page.quarantined:
            state.log(
                "crawler",
                "warning",
                f"{page.url}: quarantined {len(page.quarantined)} line(s) that look like instructions to an AI",
            )

        for link in page.internal_links:
            if link not in seen and same_site(link, start) and not looks_like_asset(link):
                seen.add(link)
                frontier.append((link, page.url))

    state.log(
        "crawler", "decision", f"stopped: {stop_reason}; {len(state.pages)} pages, {len(state.crawl_errors)} errors"
    )
    return stop_reason
