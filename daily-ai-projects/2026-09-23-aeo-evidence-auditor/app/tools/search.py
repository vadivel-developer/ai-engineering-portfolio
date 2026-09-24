"""Tool: lexical (BM25) search over crawled pages. Used by the Intent Analyst to find candidate pages.

BM25 is deliberate: a site audit has tens of pages, results must be explainable, and it needs no
embedding model or vector store."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.models import PageSnapshot

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "much",
        "my",
        "near",
        "of",
        "on",
        "or",
        "our",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    ]
)


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


def stem(token: str) -> str:
    for suffix in ("ing", "ers", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _doc_terms(page: PageSnapshot) -> list[str]:
    # Titles and headings describe what a page is *for*, so they are weighted above body copy.
    weighted = " ".join([page.title or ""] * 3 + page.h1 * 3 + page.headings * 2 + [page.text])
    return [stem(t) for t in tokens(weighted)]


@dataclass
class SearchHit:
    url: str
    score: float
    title: str | None
    coverage: float  # share of query terms found in title/H1/headings


class PageIndex:
    def __init__(self, pages: list[PageSnapshot], k1: float = 1.4, b: float = 0.75) -> None:
        self.pages = [p for p in pages if p.status == 200]
        self.docs = [Counter(_doc_terms(p)) for p in self.pages]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0
        self.k1, self.b = k1, b
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 3) -> list[SearchHit]:
        q_terms = [stem(t) for t in tokens(query)]
        hits: list[SearchHit] = []
        for page, doc, length in zip(self.pages, self.docs, self.lengths, strict=True):
            score = 0.0
            for t in q_terms:
                f = doc.get(t, 0)
                if f:
                    denom = f + self.k1 * (1 - self.b + self.b * length / self.avg_len)
                    score += self.idf.get(t, 0.0) * f * (self.k1 + 1) / denom
            if score > 0:
                cov = heading_coverage(query, page)
                # Rerank: a page whose title/headings carry the query is *about* it, not just mentioning it.
                hits.append(SearchHit(page.url, round(score * (0.5 + cov), 3), page.title, cov))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def outline(self, url: str) -> dict[str, object] | None:
        for p in self.pages:
            if p.url == url:
                return {
                    "url": p.url,
                    "title": p.title,
                    "h1": p.h1,
                    "headings": p.headings[:25],
                    "word_count": p.word_count,
                    "structured_data": p.jsonld_types,
                }
        return None


def heading_coverage(query: str, page: PageSnapshot) -> float:
    q = {stem(t) for t in tokens(query)}
    if not q:
        return 0.0
    head = {stem(t) for t in tokens(" ".join([page.title or "", *page.h1, *page.headings]))}
    return round(len(q & head) / len(q), 2)
