"""Run one audit from the command line and print a summary (useful for checking the Ollama path).

python -m scripts.cli_run            # uses AEO_LLM_PROVIDER from the environment / .env
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from app.config import load_settings
from app.llm import ModelGateway, OllamaChat
from app.models import AuditRequest, RunState
from app.orchestrator import Deps, run_audit
from app.tools.fetcher import FixtureFetcher, get_sample
from app.tools.search import tokens


async def main() -> int:
    s = load_settings()
    site = get_sample("brightwater-plumbing")
    queries = sys.argv[1:] or site.default_queries
    req = AuditRequest(queries=queries)
    gw = None
    label = "demo"
    if s.llm_provider == "ollama":
        gw = ModelGateway(
            OllamaChat(s.ollama_base_url, s.ollama_model, s.llm_timeout_s), s.max_llm_calls, s.llm_max_retries
        )
        label = f"ollama:{s.ollama_model}"
    state = RunState(id="cli", request=req, mode=label)
    if gw:
        gw.on_retry = lambda m: state.log("orchestrator", "retry", m)
    t0 = time.time()
    await run_audit(
        state,
        Deps(
            FixtureFetcher(site), gw, site.base_url, [t for t in tokens("Brightwater Plumbing")], 15, s.run_deadline_s
        ),
    )
    print(
        json.dumps(
            {
                "mode": label,
                "status": state.status,
                "error": state.error,
                "seconds": round(time.time() - t0, 1),
                "intents": [i.model_dump() for i in state.intents],
                "gap_recs": [
                    {"id": r.id, "title": r.title, "evidence": [e.model_dump() for e in r.evidence]}
                    for r in state.recommendations
                    if r.source_agent == "gap_strategist"
                ],
                "verifications": [v.model_dump() for v in state.verifications if v.verdict == "rejected"],
                "stages": [
                    {
                        "agent": m.agent,
                        "status": m.status,
                        "ms": m.elapsed_ms,
                        "calls": m.llm_calls,
                        "prompt_tokens": m.prompt_tokens,
                        "output_tokens": m.output_tokens,
                    }
                    for m in state.stages
                ],
                "retries": [t.message for t in state.trace if t.kind in ("retry", "error", "warning")],
            },
            indent=2,
            default=str,
        )
    )
    return 0 if state.status == "awaiting_review" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
