"""Tool: fetch pages. Two implementations share one interface so the crawler never knows which it has.

* FixtureFetcher serves the bundled sample site from disk (offline, deterministic, safe to host).
* HttpFetcher fetches real sites with an SSRF guard, redirect re-validation, size and type limits.
"""

from __future__ import annotations

import json
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.config import SAMPLES_DIR
from app.safety import UnsafeURLError, normalize_url, validate_public_http_url

USER_AGENT = "AEOEvidenceAuditor/0.1 (+portfolio demo; respects robots.txt)"


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    error: str | None = None


class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...
    async def allowed(self, url: str) -> bool: ...
    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- sample site


@dataclass(frozen=True)
class SampleSite:
    id: str
    name: str
    base_url: str
    description: str
    default_queries: list[str]
    root: Path


def list_samples() -> list[SampleSite]:
    sites = []
    for manifest in sorted(SAMPLES_DIR.glob("*/site.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        sites.append(
            SampleSite(
                id=manifest.parent.name,
                name=data["name"],
                base_url=data["base_url"],
                description=data["description"],
                default_queries=list(data["default_queries"]),
                root=manifest.parent,
            )
        )
    return sites


def get_sample(sample_id: str) -> SampleSite:
    for s in list_samples():
        if s.id == sample_id:
            return s
    raise KeyError(sample_id)


class FixtureFetcher:
    def __init__(self, site: SampleSite, fail_paths: set[str] | None = None) -> None:
        self.site = site
        self.fail_paths = fail_paths or set()  # lets tests simulate network failures
        self._robots = urllib.robotparser.RobotFileParser()
        robots_file = site.root / "robots.txt"
        self._robots.parse(robots_file.read_text(encoding="utf-8").splitlines() if robots_file.exists() else [])

    def _resolve(self, url: str) -> Path | None:
        p = urlparse(url)
        if p.netloc != urlparse(self.site.base_url).netloc:
            return None
        rel = p.path.strip("/")
        root = self.site.root.resolve()
        for candidate in (root / rel / "index.html", root / f"{rel}.html", root / rel):
            candidate = candidate.resolve()
            if not candidate.is_relative_to(root):  # path traversal guard
                return None
            if candidate.is_file() and candidate.suffix == ".html":
                return candidate
        return None

    async def allowed(self, url: str) -> bool:
        return self._robots.can_fetch(USER_AGENT, url)

    async def fetch(self, url: str) -> FetchResult:
        if urlparse(url).path in self.fail_paths:
            return FetchResult(url, 0, "", error="simulated network failure")
        path = self._resolve(url)
        if path is None:
            return FetchResult(url, 404, "", error="not found")
        return FetchResult(url, 200, path.read_text(encoding="utf-8"))

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- live web


class HttpFetcher:
    def __init__(self, root_url: str, timeout_s: int, max_bytes: int, max_redirects: int = 3) -> None:
        self.root_url = validate_public_http_url(root_url)
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,  # every hop is re-validated below
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        self._robots: urllib.robotparser.RobotFileParser | None = None

    async def allowed(self, url: str) -> bool:
        if self._robots is None:
            self._robots = urllib.robotparser.RobotFileParser()
            p = urlparse(self.root_url)
            try:
                r = await self._client.get(f"{p.scheme}://{p.netloc}/robots.txt")
                self._robots.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                self._robots.parse([])
        return self._robots.can_fetch(USER_AGENT, url)

    async def fetch(self, url: str) -> FetchResult:
        current = url
        try:
            for _ in range(self.max_redirects + 1):
                validate_public_http_url(current)
                async with self._client.stream("GET", current) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        current = normalize_url(resp.headers["location"], current)
                        continue
                    ctype = resp.headers.get("content-type", "")
                    if resp.status_code == 200 and "html" not in ctype:
                        return FetchResult(current, resp.status_code, "", error=f"skipped non-HTML ({ctype})")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            return FetchResult(current, resp.status_code, "", error="page exceeds size limit")
                    html = body.decode(resp.encoding or "utf-8", errors="replace")
                    return FetchResult(current, resp.status_code, html)
            return FetchResult(current, 0, "", error="too many redirects")
        except UnsafeURLError as exc:
            return FetchResult(current, 0, "", error=f"blocked: {exc}")
        except httpx.TimeoutException:
            return FetchResult(current, 0, "", error="timed out")
        except httpx.HTTPError as exc:
            return FetchResult(current, 0, "", error=f"network error: {type(exc).__name__}")

    async def aclose(self) -> None:
        await self._client.aclose()
