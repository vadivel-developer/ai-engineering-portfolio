"""Builds the reviewer-approved report. Only recommendations a human approved are included."""

from __future__ import annotations

import datetime as dt

from app.models import Evidence, RunState


def _ev(e: Evidence) -> str:
    if e.kind == "quote":
        return f'quote on {e.url}: "{e.text}"'
    if e.kind == "absent":
        return f"'{e.text}' does not appear on {e.url}"
    return f"{e.field} on {e.url} = {e.text}"


def build_markdown(state: RunState) -> str:
    approved = {d.rec_id: d for d in state.review if d.decision == "approved"}
    recs = [r for r in state.recommendations if r.id in approved]
    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: (order[r.priority.value], r.id))
    created = dt.datetime.fromtimestamp(state.created, tz=dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    target = state.site_url or state.request.start_url or state.request.sample_id
    lines = [
        "# SEO / AEO audit — approved recommendations",
        "",
        f"- Site: {target}",
        f"- Run: {state.id} · {created}",
        f"- Reasoning backend: {state.mode}",
        f"- Reviewed by: {state.reviewer or '—'}",
        f"- Pages crawled: {len(state.pages)} · technical findings: {len(state.findings)}",
        f"- Approved: {len(recs)} of {sum(1 for v in state.verifications if v.verdict == 'verified')} verified "
        "recommendations",
        "",
        "Every item below passed automated evidence verification against the crawled pages and was approved "
        "by the named reviewer.",
        "",
    ]
    for i, r in enumerate(recs, start=1):
        lines += [
            f"## {i}. {r.title}",
            "",
            f"**Priority:** {r.priority.value} · **Type:** {r.type.value}"
            + (f" · **Query:** {r.query}" if r.query else "")
            + (f" · **Page:** {r.target_url}" if r.target_url else ""),
            "",
            r.detail,
            "",
            "Evidence:",
        ]
        lines += [f"- {_ev(e)}" for e in r.evidence]
        note = approved[r.id].note
        if note:
            lines += ["", f"Reviewer note: {note}"]
        lines.append("")
    if not recs:
        lines.append("_No recommendations were approved._")
    return "\n".join(lines) + "\n"
