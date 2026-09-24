"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = ROOT / "samples"
STATIC_DIR = ROOT / "static"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the app runs without python-dotenv. Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    ollama_base_url: str
    ollama_model: str
    llm_timeout_s: int
    llm_max_retries: int
    max_llm_calls: int
    live_crawl_enabled: bool
    max_pages: int
    fetch_timeout_s: int
    max_page_bytes: int
    run_deadline_s: int
    host: str
    port: int
    log_level: str


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    provider = os.environ.get("AEO_LLM_PROVIDER", "demo").strip().lower()
    if provider not in {"demo", "ollama"}:
        provider = "demo"
    return Settings(
        llm_provider=provider,
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "qwen3:4b"),
        llm_timeout_s=_int("AEO_LLM_TIMEOUT_SECONDS", 90, 5, 600),
        llm_max_retries=_int("AEO_LLM_MAX_RETRIES", 2, 0, 5),
        max_llm_calls=_int("AEO_MAX_LLM_CALLS", 40, 1, 500),
        live_crawl_enabled=_bool("AEO_LIVE_CRAWL_ENABLED", False),
        max_pages=_int("AEO_MAX_PAGES", 25, 1, 200),
        fetch_timeout_s=_int("AEO_FETCH_TIMEOUT_SECONDS", 10, 1, 60),
        max_page_bytes=_int("AEO_MAX_PAGE_BYTES", 1_500_000, 10_000, 10_000_000),
        run_deadline_s=_int("AEO_RUN_DEADLINE_SECONDS", 600, 10, 3600),
        host=os.environ.get("AEO_HOST", "127.0.0.1"),
        port=_int("AEO_PORT", 8765, 1, 65535),
        log_level=os.environ.get("AEO_LOG_LEVEL", "INFO").upper(),
    )
