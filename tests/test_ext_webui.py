"""Tests for the fork's web UI and GitHub trust score."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from skillspector.ext import github_search  # noqa: E402
from skillspector.ext.webui import app as webui  # noqa: E402

NOW = datetime(2026, 10, 25, tzinfo=UTC)
PROBES = Path(__file__).resolve().parents[1] / "eval" / "probes"


def _repo(**overrides) -> dict:
    repo = {
        "full_name": "acme/skills",
        "html_url": "https://github.com/acme/skills",
        "description": "skills",
        "owner": {"type": "Organization"},
        "stargazers_count": 2000,
        "forks_count": 300,
        "open_issues_count": 40,
        "created_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2026-10-20T00:00:00Z",
        "archived": False,
        "license": {"spdx_id": "MIT"},
    }
    repo.update(overrides)
    return repo


def test_established_org_repo_scores_high() -> None:
    trust = github_search.score_repo(_repo(), contributors=12, now=NOW)
    assert trust.level == "high"
    assert any("organisation" in s for s in trust.signals)


def test_star_spike_on_young_repo_scores_low() -> None:
    young = _repo(
        owner={"type": "User"},
        created_at="2026-10-03T00:00:00Z",
        stargazers_count=5400,
        forks_count=900,
        open_issues_count=2,
        license=None,
    )
    trust = github_search.score_repo(young, contributors=1, now=NOW)
    assert trust.level == "low"
    assert any("star spike" in s for s in trust.signals)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(webui, "DOTENV", tmp_path / "missing.env")
    monkeypatch.setattr(webui, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(webui.pins, "PIN_DIR", tmp_path / "data" / "pins")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    webui._jobs.clear()
    return TestClient(webui.app)


def test_index_and_profiles(client: TestClient) -> None:
    assert "Skill Scanner" in client.get("/").text
    profiles = {p["id"]: p for p in client.get("/api/profiles").json()}
    assert profiles["static"]["ready"] is True
    assert profiles["sol-medium"]["ready"] is False
    assert profiles["sol-medium"]["model"] == "gpt-6.1-sol"
    assert profiles["sol-medium"]["missing"] == "OPENAI_API_KEY"
    assert profiles["local-qwen-coder-7b"]["provider"] == "ollama"
    assert all("sk-" not in str(p) for p in profiles.values())


def test_llm_profile_without_key_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/scan", json={"source": str(PROBES / "T01_direct"), "profile": "sol-medium"}
    )
    assert response.status_code == 400
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_bad_source_is_rejected(client: TestClient) -> None:
    response = client.post("/api/scan", json={"source": "/no/such/path", "profile": "static"})
    assert response.status_code == 400


def test_static_scan_job_completes(client: TestClient) -> None:
    job = client.post(
        "/api/scan", json={"source": str(PROBES / "T01_direct"), "profile": "static"}
    ).json()
    deadline = time.time() + 120
    while time.time() < deadline:
        state = client.get(f"/api/jobs/{job['id']}").json()
        if state["status"] in {"done", "error"}:
            break
        time.sleep(0.5)
    assert state["status"] == "done", state.get("error")
    assert {i["id"] for i in state["report"]["verdict"]["issues"]} >= {"P1"}
    assert state["report"]["report"]["issues"]  # upstream report is kept


def test_upload_rejects_other_file_types(client: TestClient) -> None:
    response = client.post(
        "/api/scan/upload", files={"file": ("x.exe", b"MZ")}, data={"profile": "static"}
    )
    assert response.status_code == 400


def test_finished_scan_is_saved_and_reloaded(client: TestClient, tmp_path: Path) -> None:
    job = client.post(
        "/api/scan", json={"source": str(PROBES / "T01_direct"), "profile": "static"}
    ).json()
    deadline = time.time() + 120
    while time.time() < deadline and client.get(f"/api/jobs/{job['id']}").json()["status"] not in {
        "done",
        "error",
    }:
        time.sleep(0.5)
    saved = tmp_path / "data" / "scans" / f"{job['id']}.json"
    assert saved.is_file()
    webui._jobs.clear()
    webui.load_history()
    reloaded = client.get(f"/api/jobs/{job['id']}").json()
    assert reloaded["status"] == "done"
    assert reloaded["strict"] is True
    assert reloaded["report"]["verdict"]["issues"]


def test_running_scan_is_marked_interrupted_after_restart(client: TestClient) -> None:
    stale = webui.Job(id="abc123", source="x", profile="static", status="running")
    webui._save(stale)
    webui._jobs.clear()
    webui.load_history()
    assert client.get("/api/jobs/abc123").json()["status"] == "interrupted"
    assert client.delete("/api/jobs/abc123").status_code == 200
    assert client.get("/api/jobs/abc123").status_code == 404


def test_rewrite_requires_llm_scan(client: TestClient) -> None:
    webui._jobs["s1"] = webui.Job(id="s1", source="x", profile="static", status="done", report={})
    assert client.post("/api/jobs/s1/rewrite", json={"keep": [0]}).status_code == 404
    webui._jobs["s1"].report = {"verdict": {}, "explanation": {"items": []}}
    assert client.post("/api/jobs/s1/rewrite", json={"keep": [0]}).status_code == 400


def test_pin_and_verify_through_api(client: TestClient, tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# s\n", encoding="utf-8")
    webui._jobs["p1"] = webui.Job(
        id="p1",
        source=str(skill),
        profile="static",
        status="done",
        report={"verdict": {"score": 0, "recommendation": "SAFE"}},
    )
    pinned = client.post("/api/jobs/p1/pin").json()
    assert pinned["file_count"] == 1
    assert client.get("/api/pins").json()[0]["name"] == pinned["name"]
    assert client.post(f"/api/pins/{pinned['name']}/verify").json()["clean"] is True
    (skill / "SKILL.md").write_text("# changed\n", encoding="utf-8")
    assert client.post(f"/api/pins/{pinned['name']}/verify").json()["changed"] == ["SKILL.md"]
