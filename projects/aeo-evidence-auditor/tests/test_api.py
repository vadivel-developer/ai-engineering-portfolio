from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app


@pytest.fixture
def client() -> Any:
    settings = replace(load_settings(), llm_provider="demo", live_crawl_enabled=False)
    with TestClient(create_app(settings)) as c:
        yield c


def _wait(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(100):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_health_reports_mode_honestly(client: TestClient) -> None:
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["provider"] == "demo"
    assert "no language model" in h["mode"]


def test_security_headers(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_full_review_flow(client: TestClient) -> None:
    rid = client.post("/api/runs", json={"queries": ["how much does drain cleaning cost"]}).json()["id"]
    assert client.get(f"/api/runs/{rid}/report.md").status_code in (409,)  # nothing before review
    run = _wait(client, rid)
    assert run["status"] == "awaiting_review"
    assert "text" not in run["pages"][0]  # raw page text is not sent to the browser
    verified = [v["rec_id"] for v in run["verifications"] if v["verdict"] == "verified"]
    decisions = [{"rec_id": rid_, "decision": "approved" if i == 0 else "rejected"} for i, rid_ in enumerate(verified)]

    r = client.post(f"/api/runs/{rid}/review", json={"reviewer": "Test Reviewer", "decisions": decisions[:1]})
    assert r.status_code == 422 and "missing" in r.json()["error"]  # every verified item needs a decision

    r = client.post(f"/api/runs/{rid}/review", json={"reviewer": "Test Reviewer", "decisions": decisions})
    assert r.status_code == 200 and r.json()["approved"] == 1

    report = client.get(f"/api/runs/{rid}/report.md")
    assert report.status_code == 200
    assert "Reviewed by: Test Reviewer" in report.text
    assert report.text.count("\n## ") == 1  # only the approved item

    again = client.post(f"/api/runs/{rid}/review", json={"reviewer": "x", "decisions": decisions})
    assert again.status_code == 409  # a review cannot be replayed


def test_review_rejects_unknown_or_unverified_ids(client: TestClient) -> None:
    rid = client.post("/api/runs", json={"queries": ["drain cleaning"]}).json()["id"]
    _wait(client, rid)
    r = client.post(
        f"/api/runs/{rid}/review", json={"reviewer": "x", "decisions": [{"rec_id": "NOPE", "decision": "approved"}]}
    )
    assert r.status_code == 422 and "only verified" in r.json()["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"queries": []},
        {"queries": ["   "]},
        {"queries": ["x" * 121]},
        {"queries": ["q"] * 13},
        {"queries": ["q"], "max_pages": 500},
        {"queries": ["q"], "sample_id": "../../etc"},
    ],
)
def test_input_validation(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/api/runs", json=body).status_code == 422


def test_live_crawl_disabled_by_default(client: TestClient) -> None:
    r = client.post("/api/runs", json={"source": "live", "start_url": "https://example.com/", "queries": ["q"]})
    assert r.status_code == 403


def test_live_crawl_blocks_private_targets() -> None:
    settings = replace(load_settings(), llm_provider="demo", live_crawl_enabled=True)
    with TestClient(create_app(settings)) as c:
        r = c.post("/api/runs", json={"source": "live", "start_url": "http://169.254.169.254/", "queries": ["q"]})
        assert r.status_code == 400 and "non-public" in r.json()["error"]


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_ollama_unavailable_returns_503() -> None:
    settings = replace(load_settings(), llm_provider="ollama", ollama_base_url="http://127.0.0.1:9")
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/health").json()["model_ready"] is False
        r = c.post("/api/runs", json={"queries": ["q"]})
        assert r.status_code == 503 and "unavailable" in r.json()["error"]


def test_head_requests_for_uptime_monitors(client: TestClient) -> None:
    assert client.head("/").status_code == 200
    assert client.head("/api/health").status_code == 200
