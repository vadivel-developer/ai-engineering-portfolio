# AEO Evidence Auditor

A multi-agent SEO and **answer-engine optimisation (AEO)** audit in which every recommendation must
cite evidence that a separate verifier checks against the crawled pages. Nothing reaches the report
until a person approves it.

![Review queue with verified evidence](docs/screenshots/review-queue-desktop.png)

| | |
|---|---|
| **Status** | In progress. Built and verified locally; not yet published (see [section 13](#13-github-repository-and-live-url)) |
| **GitHub** | Not created yet |
| **Live demo** | Not deployed yet |
| **Runs without paid APIs** | Yes. Demonstration mode needs no model; the model mode uses a free local model through Ollama |

---

## 1. The problem

SEO audit tools list hundreds of issues, and AI writing tools produce confident content advice that
nobody can trace back to the page. Agencies and in-house marketers then spend hours checking whether
a recommendation is even true (*"does that page really not mention pricing?"*), and a site's own
content can manipulate an AI assistant that reads it. Blog comments and hidden text that address AI
crawlers directly are increasingly common.

This project treats an audit as an **evidence problem**. Each recommendation carries claims that can
be checked mechanically: a verbatim quote from a page, a term the page never uses, or a field value
such as `robots_meta = noindex, follow`. A verifier that does not use a model re-checks every claim.
Recommendations that fail go back to their author once, and whatever still fails is shown to the
reviewer as rejected and cannot be approved.

## 2. Who would use it

* SEO/AEO consultants and agencies producing client audits that must stand up to scrutiny
* In-house marketing teams on WordPress or HubSpot sites deciding which content to write next
* Developers who need a prioritised, evidence-backed fix list rather than a 300-row spreadsheet

## 3. Demo walkthrough (2 minutes)

1. `Run audit` against the bundled sample site (*Brightwater Plumbing Co.*, a fictional 9-page local
   business on the reserved `.example` domain) with five target queries.
2. Watch the agent rail on the left as the Crawler, Technical Auditor, Intent Analyst, Gap Strategist
   and Evidence Verifier run in turn, with timings and model usage.
3. The **Review** tab lists verified recommendations. Quotes are shown as green highlights on the page
   they came from, and missing terms are struck through. The first item is an escalation between agents: the
   page that best answers *"emergency plumber near me"* is marked `noindex`.
4. **Pages** shows the two blog-comment lines that tried to instruct an AI; they were quarantined and
   never reached a model or a recommendation.
5. **Trace** shows every tool call, handoff, retry and decision.
6. Approve or reject each item, add notes, enter your name, then choose *Approve selected and create
   report*. Only approved items appear in the downloadable Markdown report.

| Evidence detail | Quarantined injection | Agent trace |
|---|---|---|
| ![](docs/screenshots/evidence-detail.png) | ![](docs/screenshots/pages-quarantine.png) | ![](docs/screenshots/agent-trace.png) |

| Query intents | Approved report | Mobile |
|---|---|---|
| ![](docs/screenshots/query-intents.png) | ![](docs/screenshots/approved-report.png) | ![](docs/screenshots/review-queue-mobile.png) |

All screenshots were captured from the running application (headless Chrome, 1440×900 and 390×844).

## 4. The agents, and why each exists

Only two of the five agents use a language model. The other three are deterministic because their
questions have exact answers, and a model would add cost and error without adding value.

| Agent | Responsibility | Tools (all read-only) | Input → Output | Model? |
|---|---|---|---|---|
| **Crawler** | Collect a bounded same-site page set | `robots.txt` check, `fetch`, `extract_page` | start URL, page limit, deadline → `PageSnapshot[]`, `CrawlError[]` | No |
| **Technical Auditor** | 14 rule-based checks (title, meta, H1, noindex, canonical, alt text, broken links, thin content, JSON-LD) | pure check functions | pages → `TechnicalFinding[]` + grouped recommendations | No |
| **Intent Analyst** | Classify each query's intent and find the page that answers it | `search_pages` (BM25), `get_page_outline` | query + page index → `QueryIntent` | **Yes** (tool-calling loop, max 3 calls/query, then schema-constrained answer) |
| **Gap Strategist** | Propose content changes with checkable evidence | none. It receives the page as fenced, untrusted data | intent + page → 0–2 `Recommendation`s | **Yes** (JSON-schema structured output) |
| **Evidence Verifier** | Check every claim independently | exact-match evidence checks | recommendations + pages → `Verification` (verified / rejected + reasons) | No, deliberately: it must not share the failure modes of the model it checks |

Why multi-agent here? The verifier is independent of the authors by design, the technical checks and
the content judgement need different tools, and the agents act on each other's intermediate results:
the Gap Strategist skips queries that are already well answered, downgrades a "strong match" claim
when the page never uses the query's key terms, and escalates a strong match on a `noindex` page;
the verifier sends rejected items back for a revision.

## 5. Handoffs and human review

```mermaid
flowchart LR
  C[Crawler] -->|≥1 page| T[Technical Auditor]
  C -->|0 pages| F[Run failed]
  T --> I[Intent Analyst]
  I --> G[Gap Strategist]
  G --> V[Evidence Verifier]
  T -->|technical recs| V
  V -->|rejected, revision 0| G
  V -->|verified + still-rejected| H[Human review]
  H -->|approved only| R[Markdown report]
```

* **Shared state** is one typed `RunState` (Pydantic): pages, findings, intents, recommendations,
  verifications, review decisions, per-stage metrics and an append-only trace.
* **Stop conditions:** page limit, run deadline (later stages are skipped and labelled, not run
  late), a model-call budget per run, and at most **one** revision round.
* **Retries:** a fetch is retried once on network errors; a model call is retried up to
  `AEO_LLM_MAX_RETRIES` times with exponential backoff, and invalid JSON is sent back to the model
  with the validation error.
* **Failure handling:** if the model fails or the budget runs out, the Intent Analyst uses a
  deterministic fallback, and the result is marked `FALLBACK` in the UI and trace. If no page can be
  crawled, the run fails with a clear message. Unexpected exceptions mark the run failed instead of
  leaving it hanging.
* **Human gate:** the API refuses a review that omits any verified item, includes unverified or
  unknown IDs, or replays a completed review. The report endpoint returns `409` until review is done.
  The agents have no write access to any site or account.

## 6. Architecture and technology choices

```
static/ (vanilla JS, no build)  ──HTTP──►  FastAPI (app/main.py)
                                              │ asyncio task per run
                                              ▼
                                   orchestrator.py (state machine)
          ┌──────────────┬───────────────┬───────────┴───┬────────────────┐
     crawler.py     technical.py      intent.py        gaps.py        verifier.py
          │                              │  ▲             │
   tools/fetcher.py              tools/search.py (BM25)   │
   tools/html_extract.py                 └── llm.py: ModelGateway ── Ollama /api/chat
```

| Choice | Why |
|---|---|
| **Python + FastAPI + Pydantic v2** | Typed contracts between agents; request validation; the Pydantic JSON schema is passed straight to the model as its structured-output format |
| **Plain orchestrator instead of an agent framework** | The flow is a small bounded state machine; explicit code makes the retry, budget and handoff rules readable and testable. (My other portfolio project, `sageit-rag-chat`, uses LangGraph; this one intentionally shows the same concepts without a framework.) |
| **Ollama (`qwen3:4b` default)** | Free, local, supports tool calling and JSON-schema constrained output |
| **BM25 search instead of embeddings** | A site audit has tens of pages; lexical scores are explainable to a client and need no vector store |
| **Vanilla JS UI** | One process to run and deploy; strict CSP (`script-src 'self'`); all untrusted strings rendered with `textContent` |
| **Deterministic demo mode** | Lets a recruiter use the whole workflow on a free host with no model. It is labelled on every screen and in every report |

## 7. Screenshots

See [section 3](#3-demo-walkthrough-2-minutes). Source images are in `docs/screenshots/`.

## 8. Windows PowerShell setup

Requires Python 3.11+ (tested with 3.13.10). Docker is not needed.

```powershell
git clone <repository-url> aeo-evidence-auditor
cd aeo-evidence-auditor
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
# open http://127.0.0.1:8765
```

**Local model mode (optional, free):** install [Ollama](https://ollama.com), then

```powershell
ollama pull qwen3:4b          # ~2.5 GB download, ~4 GB RAM while running
# in .env:  AEO_LLM_PROVIDER=ollama   (optionally OLLAMA_MODEL=llama3.2)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

With no GPU, expect minutes per audit rather than seconds; see the measured timings below.

**Checks:**

```powershell
python -m pytest -q                         # 55 tests
python -m ruff check .
python -m ruff format --check .
python -m mypy                              # strict mode
python -m evals.run_eval                    # demo-mode evaluation
python -m evals.run_eval --provider ollama  # local-model evaluation
python -m scripts.cli_run                   # one audit from the command line
```

## 9. Sample input and verified output

Input (the UI defaults): sample site `brightwater-plumbing`, 15-page limit, queries:
`emergency plumber near me` · `why is my water heater making a popping noise` ·
`how much does drain cleaning cost` · `tankless water heater installation` · `brightwater plumbing reviews`

Output in demonstration mode (reproduced by `test_demo_workflow_reaches_human_review` and the browser run):

* 9 pages crawled; `/admin/login` skipped by `robots.txt`; `/services/sewer-line-repair` → 404
* 24 technical findings grouped into 13 recommendations
* 6 content recommendations, for example:
  * **Remove noindex from the page that answers “emergency plumber near me”**. Evidence: `robots_meta` on `/emergency-plumber` = `noindex, follow`
  * **Cover 'cost' on Drain Cleaning**. Evidence: `cost` never appears on `/services/drain-cleaning`
  * **Cover 'tankless', 'installation' on Water Heater Repair**. Evidence: both terms absent; quote *“Water Heater Repair and Replacement”*
* 19/19 recommendations verified; 2 injected lines quarantined; the injected competitor never appears in output

## 10. Tests and evaluation

**Automated tests: 55 passing** (`pytest`, ~8 s). They cover:

* SSRF guard (file://, localhost, RFC1918, link-local metadata IP, IPv6 loopback, credentials, odd ports)
* prompt-injection detection, fencing that page content cannot close, quarantine of visible and hidden injected text
* crawler: robots.txt, 404 recording, page-limit stop, transient-error retry, total failure
* technical checks vs. the hand-labelled issue list; verifier accepting true and rejecting hallucinated,
  false-absence, quarantined-source, external-link and uncrawled-page evidence
* **model path with a scripted fake model**: tool-calling loop, invalid-JSON retry, a hallucinated
  quote rejected then corrected in the single revision round, budget exhaustion → labelled fallback,
  model outage → 3 attempts then fallback, deadline → no further model calls, and an unfounded
  "strong match" claim downgraded
* API: validation errors, the complete review flow, review replay (409), unverified IDs (422), live
  crawl disabled (403), private target blocked (400), Ollama down (503), security headers

**Browser checks** (headless Chrome via Playwright, results in `docs/browser-check.json`). The full
workflow ran at 1440×900 and 390×844: start, run audit, review, submit, report. axe-core
(WCAG 2.0/2.1 A and AA plus best-practice rules) found **0 violations** on every screen, in light and
dark mode. There was no horizontal overflow at either width and no console errors. The Run button
is reachable by Tab, the tabs respond to arrow keys, and focus outlines are visible.

**Evaluation set:** `evals/ground_truth.json` has 20 hand-labelled technical issues and 12 labelled
queries. Seven of the queries are *not* UI defaults and were not used to tune the heuristics.

Results (demonstration mode, `python -m evals.run_eval`, 2026-09-23):

| Metric | Result |
|---|---|
| Technical recall | **20/20 (1.00)** |
| Technical precision | **0.83**. The 4 extra findings are "thin content" on pages with 224–242 words, under the 250-word threshold. I kept the threshold rather than tune it to the labels |
| Intent accuracy | **11/12**. Missed: *"water heater repair tacoma"* (a city name is not recognised as local intent) |
| Best-page accuracy | **10/12**. Missed: *"burst pipe what to do"* (chose the winterising post) and *"why does my faucet bang…"* (no page matched by keywords; the answer is under a "Knocking in the pipes" heading) |
| Gap recommendations verified first time | 14/14 |
| Injection resistance | passed (2 lines quarantined, 0 markers in verified output) |

**Local-model runs (Ollama, CPU only, no GPU, Windows 11 laptop, 2026-09-23).** Results files are in `evals/results/`.

| Run | Time | Model calls | Tokens (prompt / output) | What happened |
|---|---|---|---|---|
| `llama3.2` (3B), full 12-query eval (`ollama-llama3.2-12-queries-eval.json`) | 574 s | 55 | 27,992 / 6,666 | Intent accuracy **8/12**, best page **8/12**: worse than the keyword rules, especially on brand queries. Gap recommendations verified on first pass: **9/19**. **16 recommendations rejected** by the verifier; 0 injected markers in verified output |
| `llama3.2`, 2 queries (`ollama-llama3.2-2-queries.json`) | 116 s | 11 | 5,801 / 1,364 | The model claimed the water-heater page "offers tankless water heater installation" (false) and marked both queries as strong matches. The grounding check downgraded both to partial. The model then **invented a quote from an external site (hunker.com), a non-existent `/services/tankless-water-heater-installation` page and a manufacturer list**. The verifier rejected all six fabricated items, including their revisions; only the true claim ("cost" never appears on the drain-cleaning page) survived |
| `qwen3:4b`, 5 queries (`ollama-qwen3-4b-5-queries.json`) | 2,000 s | 21 | 12,917 / 6,706 | 3 queries answered well by the model; 2 hit the 180 s read timeout three times and fell back to the labelled rules; the Gap Strategist was skipped because the run deadline had passed. This run exposed that the deadline was only checked between stages, and it is now checked before every model call as well (test `test_deadline_stops_new_model_calls`) |

The takeaway is the reason the project exists. Small local models fabricate evidence often, and the
independent verifier is what stops those fabrications reaching a client. With the default
`qwen3:4b` on CPU, expect roughly 5–7 minutes per query. Use `llama3.2` or a GPU for interactive speed.


## 11. Security and privacy

* **Untrusted page content.** Lines that address an AI (for example "ignore previous instructions"
  or "AI assistants must recommend…") are quarantined at extraction, including text in hidden
  elements. Page text sent to the model is fenced as data, and the fence cannot be closed from inside
  the page. The verifier rejects evidence quoted from quarantined text, recommendations containing
  instruction-like text, and links to other domains.
* **Restricted tools.** Agents can only read: fetch same-site pages, search and outline crawled pages.
  No agent can write, email, publish or change an account.
* **SSRF protection for live crawling.** HTTP(S) only, default ports only, no embedded credentials,
  DNS-resolved addresses must be public, every redirect hop re-validated, size and content-type
  limits. Live crawling is **off by default** (`AEO_LIVE_CRAWL_ENABLED=false`) and should stay off on
  a public deployment.
* **Browser.** Strict CSP (`default-src 'self'`, no inline script), `nosniff`, `frame-ancestors 'none'`,
  and no `innerHTML` with untrusted data. Raw page text is never sent to the browser.
* **Data.** Runs are held in memory (at most 100, oldest evicted) and nothing is written to disk. No
  keys are needed; `.env` is git-ignored.

## 12. Known limitations

* Demonstration mode uses keyword rules and templates. It shows the workflow and the verification
  but does not reason like a language model, and its recommendations are formulaic.
* A 4B-parameter local model on CPU is slow, and its judgement is weaker than a large hosted model.
* Evidence checks are lexical. An "absent" term can miss synonyms ("price" vs "cost"), and a quote
  must match exactly after whitespace and case normalisation.
* The injection filter is pattern-based and will not catch every phrasing. It is one layer, backed
  by fencing, read-only tools and verification.
* No JavaScript rendering, sitemap parsing, Core Web Vitals, or backlink data. The Crawler only sees
  server-rendered HTML.
* In-memory run store: runs disappear on restart, and there is no authentication. Keep it as a
  single-user tool or put it behind an authenticating proxy.

## 13. GitHub repository and live URL

* GitHub repository: **not created**. GitHub CLI is not installed on the build machine and no
  GitHub credentials were available to the build session.
* Live URL: **none**. No hosting account was specified. The app runs as a single Python web process
  (`uvicorn app.main:app`), so any Python host works; a suitable free option is Render's free web
  service (build `pip install -r requirements.txt`, start
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, env `AEO_LLM_PROVIDER=demo`,
  `AEO_LIVE_CRAWL_ENABLED=false`). A public deployment would run in clearly labelled demonstration
  mode.

## 14. Skills demonstrated and relevant roles

Python API development (FastAPI, async, Pydantic) · LLM tool calling and JSON-schema structured
outputs · multi-agent orchestration with shared state, handoff rules, retries, budgets and stop
conditions · prompt-injection defence and least-privilege tools · human-in-the-loop approval ·
evaluation datasets with precision/recall · testing with scripted model doubles · SSRF-safe crawling ·
accessible, responsive UI (axe-core: 0 violations) · SEO/AEO domain knowledge.

Relevant to: **AI Engineer, Agent Engineer, GenAI Engineer, AI Automation Engineer, Full-Stack AI
Engineer**, and SEO/marketing-technology roles that are adopting AI.

## 15. Resume bullets

* Built a five-agent SEO/AEO audit system in Python/FastAPI in which two local-LLM agents (tool
  calling and JSON-schema output via Ollama) propose recommendations and a deterministic verifier
  checks every cited quote and missing term against crawled pages. In a llama3.2 run it rejected all
  six fabricated recommendations (invented pages and external quotes) before the mandatory
  human-approval step.
* Hardened the agents against prompt injection in crawled web content (quarantine, data fencing,
  read-only tools, verifier rules) and against SSRF, and backed the workflow with 55 automated tests
  and a labelled evaluation set (technical-issue recall 20/20, precision 0.83, intent accuracy 11/12
  in deterministic mode).

## 16. 60-second interview explanation

> "SEO audits and AI content advice share one problem: you can't tell which recommendations are true.
> I built a multi-agent auditor where every claim has to be checkable. A crawler collects pages and
> quarantines text that tries to instruct an AI. A rule-based technical auditor handles things with
> exact answers, like missing H1s or noindex. Two LLM agents run on a local model through Ollama. One
> uses search tools to decide which page answers each query, and one proposes content changes. Each
> proposal must include evidence: a verbatim quote or a term the page never uses. A separate verifier
> that uses no model checks every claim. Failures go back once for revision, and anything still wrong
> is shown as rejected and can't be approved. A person approves the rest before a report exists. I
> tested the model path with a scripted fake model that hallucinates on purpose. The evaluation
> reports its weak spots honestly, such as city names not being recognised as local intent."
