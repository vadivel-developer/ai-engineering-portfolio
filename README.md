# AI Engineering Portfolio

Working agent applications, built one at a time. I'm moving from web development (websites, frontend,
WordPress, HubSpot, SEO and integrations) into AI and agent engineering. Each project here connects
that background to a skill an employer can check in running code: tool-using agents, bounded
orchestration, evaluation, security and human review.

Every project:

* runs locally without a paid AI API key (a free local model, or a clearly labelled demonstration mode)
* has automated tests, and an evaluation set where output quality matters
* states what was verified and what was not

## Projects

| Date | Project | Domain | Agent roles | Technologies | GitHub | Live | Verification |
|---|---|---|---|---|---|---|---|
| 2026-09-23 | [AEO Evidence Auditor](https://github.com/vadivel-developer/ai-engineering-portfolio/tree/main/daily-ai-projects/2026-09-23-aeo-evidence-auditor) | SEO / answer-engine optimisation | Crawler, Technical Auditor, Intent Analyst (LLM + tools), Gap Strategist (LLM), Evidence Verifier, human reviewer | Python 3.13, FastAPI, Pydantic v2, Ollama (qwen3:4b), BM25, vanilla JS | [aeo-evidence-auditor](https://github.com/vadivel-developer/aeo-evidence-auditor) | not deployed yet | Local: 55 tests, ruff, mypy --strict, eval set, desktop and mobile browser run, axe-core 0 violations. **In progress** (on GitHub; not yet deployed) |

The full list with status and blockers is in [daily-ai-projects/PROJECT_TRACKER.md](https://github.com/vadivel-developer/ai-engineering-portfolio/blob/main/daily-ai-projects/PROJECT_TRACKER.md).

### AEO Evidence Auditor (2026-09-23)

A multi-agent SEO/AEO audit where every recommendation must cite a verbatim quote, a missing term or
a field value. A verifier that uses no model checks each claim against the crawled pages, rejected
items get one revision, and nothing reaches the report without a person's approval. Crawled pages are
treated as untrusted: lines that try to instruct an AI are quarantined before any model sees them.

![AEO Evidence Auditor review queue](https://raw.githubusercontent.com/vadivel-developer/ai-engineering-portfolio/main/daily-ai-projects/2026-09-23-aeo-evidence-auditor/docs/screenshots/review-queue-desktop.png)
