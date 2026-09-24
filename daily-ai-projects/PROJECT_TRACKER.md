# Daily AI projects: tracker

One complete agent project per entry. A project is **Complete** only when the app works, its
documented checks pass, its GitHub repository exists, and its live URL has been verified.
Otherwise it is **In progress** with the exact blocker listed.

| Date | Project | Domain | Agent architecture | Stack | GitHub | Live URL | Status |
|---|---|---|---|---|---|---|---|
| 2026-09-23 | [AEO Evidence Auditor](2026-09-23-aeo-evidence-auditor/) | SEO / AEO | 5 agents: Crawler → Technical Auditor → Intent Analyst (LLM, tool calling) → Gap Strategist (LLM, structured output) → Evidence Verifier (with a revision loop back to Gap Strategist) → human review | Python, FastAPI, Pydantic, Ollama, BM25, vanilla JS | [aeo-evidence-auditor](https://github.com/vadivel-developer/aeo-evidence-auditor) | not deployed | **In progress**: local build verified and on GitHub; blocked on choosing a hosting account for the live URL |

## Coverage so far

Use this to avoid repeating a use case or an architecture.

| Skill | Shown in |
|---|---|
| Python API development | 2026-09-23 |
| LLM tool calling + JSON-schema structured output | 2026-09-23 |
| Orchestration with shared state, retries, budgets, stop conditions | 2026-09-23 (hand-written state machine, no framework) |
| Prompt-injection defence, read-only tools | 2026-09-23 |
| Human approval gate | 2026-09-23 |
| Evaluation dataset (precision / recall / accuracy) | 2026-09-23 |
| Accessibility + responsive UI (axe-core checked) | 2026-09-23 |
| TypeScript full-stack | not yet |
| RAG ingestion / citations / retrieval eval | not yet (outside this repo: `sageit-rag-chat`) |
| MCP server or client | not yet |
| Deployment + monitoring | not yet |

## Candidates for later entries

Different domains and different technical approaches from the entries above.

* Customer-support triage in TypeScript with an MCP knowledge-base server and escalation rules
* Cybersecurity alert triage with evidence collection and a human-approved response plan
* Video transcript → scene plan + subtitle QA pipeline
* Invoice/PO reconciliation with exception handling and an approval queue
