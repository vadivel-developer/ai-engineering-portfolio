"""HTTP API + static UI."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import STATIC_DIR, Settings, load_settings
from app.llm import ModelGateway, OllamaChat, ollama_available
from app.models import AuditRequest, ReviewSubmission, RunState, RunStatus
from app.orchestrator import Deps, run_audit
from app.report import build_markdown
from app.safety import UnsafeURLError, validate_public_http_url
from app.tools.fetcher import Fetcher, FixtureFetcher, HttpFetcher, get_sample, list_samples
from app.tools.search import tokens

MAX_STORED_RUNS = 100
MAX_ACTIVE_RUNS = 2
GENERIC_HOST_PARTS = {"www", "com", "org", "net", "co", "io", "uk", "example", "inc", "llc", "ltd", "company"}
DEMO_LABEL = "Demonstration mode · deterministic rules, no language model"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("aeo.api")

    app = FastAPI(title="AEO Evidence Auditor", version="0.1.0", docs_url="/api/docs", redoc_url=None)
    runs: dict[str, RunState] = {}
    tasks: dict[str, asyncio.Task[None]] = {}
    app.state.runs = runs
    app.state.tasks = tasks
    app.state.settings = settings

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        return response

    async def backend() -> tuple[str, bool, str]:
        if settings.llm_provider == "ollama":
            ok, detail = await ollama_available(settings.ollama_base_url, settings.ollama_model)
            return f"Local model · {settings.ollama_model} via Ollama", ok, detail
        return DEMO_LABEL, True, "ok"

    @app.api_route("/api/health", methods=["GET", "HEAD"])
    async def health() -> dict[str, Any]:
        label, ready, detail = await backend()
        return {
            "status": "ok" if ready else "degraded",
            "provider": settings.llm_provider,
            "mode": label,
            "model_ready": ready,
            "detail": detail,
            "live_crawl_enabled": settings.live_crawl_enabled,
            "active_runs": sum(1 for t in tasks.values() if not t.done()),
        }

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        label, ready, detail = await backend()
        return {
            "mode": label,
            "provider": settings.llm_provider,
            "model_ready": ready,
            "model_detail": detail,
            "live_crawl_enabled": settings.live_crawl_enabled,
            "max_pages": settings.max_pages,
            "samples": [
                {
                    "id": s.id,
                    "name": s.name,
                    "base_url": s.base_url,
                    "description": s.description,
                    "default_queries": s.default_queries,
                }
                for s in list_samples()
            ],
        }

    @app.post("/api/runs", status_code=202)
    async def create_run(req: AuditRequest) -> dict[str, str]:
        if sum(1 for t in tasks.values() if not t.done()) >= MAX_ACTIVE_RUNS:
            raise HTTPException(429, "Two audits are already running. Try again when one finishes.")
        label, ready, detail = await backend()
        if not ready:
            raise HTTPException(503, f"Model backend unavailable: {detail}")

        fetcher: Fetcher
        if req.source == "live":
            if not settings.live_crawl_enabled:
                raise HTTPException(403, "Live crawling is disabled on this server. Use the sample site.")
            if not req.start_url:
                raise HTTPException(422, "start_url is required for a live audit")
            try:
                start_url = validate_public_http_url(req.start_url)
            except UnsafeURLError as exc:
                raise HTTPException(400, f"URL rejected: {exc}") from exc
            fetcher = HttpFetcher(start_url, settings.fetch_timeout_s, settings.max_page_bytes)
            host = start_url.split("//", 1)[-1].split("/")[0].replace("-", " ").replace(".", " ")
            brand = [t for t in tokens(host) if t not in GENERIC_HOST_PARTS]
        else:
            try:
                site = get_sample(req.sample_id)
            except KeyError as exc:
                raise HTTPException(404, "unknown sample site") from exc
            start_url = site.base_url
            fetcher = FixtureFetcher(site)
            brand = [t for t in tokens(site.name.split("(")[0]) if t not in GENERIC_HOST_PARTS]

        run_id = secrets.token_urlsafe(8)
        state = RunState(id=run_id, request=req, mode=label, site_url=start_url)
        gateway = None
        if settings.llm_provider == "ollama":
            gateway = ModelGateway(
                OllamaChat(settings.ollama_base_url, settings.ollama_model, settings.llm_timeout_s),
                max_calls=settings.max_llm_calls,
                max_retries=settings.llm_max_retries,
                on_retry=lambda msg: state.log("orchestrator", "retry", msg),
            )
        deps = Deps(
            fetcher=fetcher,
            gateway=gateway,
            start_url=start_url,
            brand_terms=brand,
            max_pages=min(req.max_pages, settings.max_pages),
            deadline_s=settings.run_deadline_s,
        )

        if len(runs) >= MAX_STORED_RUNS:  # evict oldest finished run
            oldest = min(
                (r for r in runs.values() if r.status != RunStatus.running), key=lambda r: r.created, default=None
            )
            if oldest:
                runs.pop(oldest.id, None)
                tasks.pop(oldest.id, None)
        runs[run_id] = state
        tasks[run_id] = asyncio.create_task(run_audit(state, deps))
        log.info("run %s started source=%s queries=%d mode=%s", run_id, req.source, len(req.queries), label)
        return {"id": run_id}

    def _get(run_id: str) -> RunState:
        state = runs.get(run_id)
        if state is None:
            raise HTTPException(404, "run not found")
        return state

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        state = _get(run_id)
        data = state.model_dump(mode="json", exclude={"pages": {"__all__": {"text"}}})
        for s, m in zip(data["stages"], state.stages, strict=True):
            s["elapsed_ms"] = (
                m.elapsed_ms if m.ended else (int((time.time() - m.started) * 1000) if m.started else None)
            )
        return data

    @app.post("/api/runs/{run_id}/review")
    async def review(run_id: str, sub: ReviewSubmission) -> dict[str, Any]:
        state = _get(run_id)
        if state.status != RunStatus.awaiting_review:
            raise HTTPException(409, f"run is {state.status.value}, not awaiting review")
        verified = {v.rec_id for v in state.verifications if v.verdict == "verified"}
        ids = [d.rec_id for d in sub.decisions]
        if len(set(ids)) != len(ids):
            raise HTTPException(422, "duplicate decisions for the same recommendation")
        not_allowed = [i for i in ids if i not in verified]
        if not_allowed:
            raise HTTPException(422, f"only verified recommendations can be reviewed: {', '.join(not_allowed)}")
        missing = verified - set(ids)
        if missing:
            raise HTTPException(
                422, f"a decision is required for every verified recommendation ({len(missing)} missing)"
            )
        state.review = sub.decisions
        state.reviewer = " ".join(sub.reviewer.split())
        approved = sum(1 for d in sub.decisions if d.decision == "approved")
        hr = state.stage("human_review")
        hr.status, hr.ended = "done", time.time()
        state.log(
            "human_review",
            "decision",
            f"{state.reviewer} approved {approved} and rejected {len(sub.decisions) - approved}",
        )
        state.status = RunStatus.completed
        state.log("orchestrator", "done", "report released")
        log.info("run %s reviewed approved=%d", run_id, approved)
        return {"status": state.status.value, "approved": approved}

    @app.get("/api/runs/{run_id}/report.md")
    async def report(run_id: str) -> PlainTextResponse:
        state = _get(run_id)
        if state.status != RunStatus.completed:
            raise HTTPException(409, "the report is available after human review")
        return PlainTextResponse(
            build_markdown(state),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="aeo-audit-{run_id}.md"'},
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = load_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_level=s.log_level.lower())
