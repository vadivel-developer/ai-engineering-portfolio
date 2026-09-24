"""Tool: turn raw HTML into a PageSnapshot. Pure function, no network, no model."""

from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.models import PageSnapshot
from app.safety import normalize_url, same_site, split_quarantine

MAX_TEXT_CHARS = 20_000


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text or None


def _is_hidden(el: Tag) -> bool:
    style = str(el.get("style", "")).replace(" ", "").lower()
    return (
        el.has_attr("hidden")
        or "display:none" in style
        or "visibility:hidden" in style
        or str(el.get("aria-hidden", "")).lower() == "true"
    )


def _jsonld_types(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out.extend(str(x) for x in t)
        for v in node.values():
            _jsonld_types(v, out)
    elif isinstance(node, list):
        for v in node:
            _jsonld_types(v, out)


def extract_page(url: str, status: int, html: str) -> PageSnapshot:
    soup = BeautifulSoup(html, "html.parser")

    title = _clean(soup.title.string if soup.title else None)
    meta_desc = None
    robots = None
    for m in soup.find_all("meta"):
        name = str(m.get("name", "")).lower()
        if name == "description":
            meta_desc = _clean(str(m.get("content", "")))
        elif name == "robots":
            robots = _clean(str(m.get("content", "")))
    canonical = None
    for link in soup.find_all("link"):
        raw_rel: object = link.get("rel") or []
        rel = (
            raw_rel.split()
            if isinstance(raw_rel, str)
            else [str(r) for r in raw_rel]
            if isinstance(raw_rel, list)
            else []
        )
        if "canonical" in [r.lower() for r in rel] and link.get("href"):
            canonical = normalize_url(str(link["href"]), url)

    types: list[str] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            _jsonld_types(json.loads(script.string or ""), types)
        except (json.JSONDecodeError, TypeError):
            types.append("InvalidJSON-LD")

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        target = normalize_url(href, url)
        if same_site(target, url) and target not in links:
            links.append(target)

    images_missing_alt = [
        normalize_url(str(img.get("src", "")), url)
        for img in soup.find_all("img")
        if not str(img.get("alt", "")).strip()
    ]

    # Site chrome repeats on every page; it is not what a page is about, and it would drown out the
    # page's own copy in search scoring and gap analysis.
    for el in soup(["script", "style", "noscript", "template", "svg", "header", "nav", "footer"]):
        el.decompose()

    # Hidden elements never reach the reader, so they are removed from page text. Their content is
    # still scanned: hidden text is the most common carrier for instructions aimed at AI crawlers.
    hidden_lines: list[str] = []
    for el in soup.find_all(True):
        if isinstance(el, Tag) and not el.decomposed and _is_hidden(el):
            hidden_lines.extend(ln for ln in (_clean(x) for x in el.get_text("\n").split("\n")) if ln)
            el.decompose()

    h1 = [t for t in (_clean(h.get_text(" ")) for h in soup.find_all("h1")) if t]
    headings = [t for t in (_clean(h.get_text(" ")) for h in soup.find_all(["h2", "h3"])) if t]

    body = soup.body or soup
    visible_lines = [ln for ln in (_clean(x) for x in body.get_text("\n").split("\n")) if ln]
    kept, quarantined = split_quarantine(visible_lines)
    _, hidden_flagged = split_quarantine(hidden_lines)
    text = "\n".join(kept)[:MAX_TEXT_CHARS]

    return PageSnapshot(
        url=url,
        status=status,
        title=title,
        meta_description=meta_desc,
        canonical=canonical,
        robots_meta=robots,
        h1=h1,
        headings=headings,
        jsonld_types=sorted(set(types)),
        images_missing_alt=images_missing_alt,
        internal_links=links,
        word_count=len(text.split()),
        text=text,
        quarantined=(quarantined + hidden_flagged)[:20],
    )
