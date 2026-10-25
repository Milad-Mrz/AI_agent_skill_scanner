"""Local FastAPI web UI for scanning agent skills.

Scans run the ``skillspector`` CLI in a subprocess with the environment of the
selected LLM profile, so the UI never imports untrusted skill content into this
process. The page escapes everything it shows and never renders skill markdown.

Bind to localhost only: the UI accepts local paths and spends API credit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from skillspector.ext import github_search
from skillspector.ext.llm_profiles import PROFILES, REQUIRED_KEY, profile_env, read_dotenv

STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[4]
DOTENV = Path(os.environ.get("SKILLSPECTOR_UI_DOTENV", REPO_ROOT / ".env"))
SCAN_TIMEOUT_SECONDS = 900
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
STATIC_PROFILE = "static"


@dataclass
class Job:
    id: str
    source: str
    profile: str
    status: str = "queued"  # queued | running | done | error
    started: float = field(default_factory=time.time)
    finished: float | None = None
    report: dict | None = None
    error: str | None = None
    cleanup: Path | None = None

    def summary(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "profile": self.profile,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "score": (self.report or {}).get("risk_assessment", {}).get("score"),
            "recommendation": (self.report or {}).get("risk_assessment", {}).get("recommendation"),
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2)

app = FastAPI(title="Skill Scanner", docs_url="/api/docs", redoc_url=None)


def _scan_env(profile: str) -> dict[str, str]:
    if profile == STATIC_PROFILE:
        return dict(os.environ)
    return profile_env(profile, dotenv=DOTENV)


def _run_scan(job: Job, target: str) -> None:
    job.status = "running"
    workdir = Path(tempfile.mkdtemp(prefix="skillui-"))
    report_path = workdir / "report.json"
    cmd = ["skillspector", "scan", target, "--format", "json", "--output", str(report_path)]
    if job.profile == STATIC_PROFILE:
        cmd.append("--no-llm")
    try:
        result = subprocess.run(
            cmd,
            env=_scan_env(job.profile),
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT_SECONDS,
            check=False,
        )
        if not report_path.is_file():
            raise RuntimeError(
                f"scan failed (exit {result.returncode}): {result.stderr.strip()[-800:]}"
            )
        job.report = json.loads(report_path.read_text(encoding="utf-8"))
        # Warnings such as a degraded LLM stage matter to the reader; keep them.
        job.report["_ui_warnings"] = [
            line for line in result.stderr.splitlines() if "WARNING" in line or "ERROR" in line
        ][-20:]
        job.status = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as the job error
        job.error = str(exc)
        job.status = "error"
    finally:
        job.finished = time.time()
        shutil.rmtree(workdir, ignore_errors=True)
        if job.cleanup:
            shutil.rmtree(job.cleanup, ignore_errors=True)


def _submit(source: str, target: str, profile: str, cleanup: Path | None = None) -> Job:
    if profile != STATIC_PROFILE and profile not in PROFILES:
        raise HTTPException(400, f"unknown profile {profile!r}")
    if profile != STATIC_PROFILE:
        try:
            profile_env(profile, dotenv=DOTENV)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    job = Job(id=uuid.uuid4().hex[:12], source=source, profile=profile, cleanup=cleanup)
    with _lock:
        _jobs[job.id] = job
    _executor.submit(_run_scan, job, target)
    return job


class ScanRequest(BaseModel):
    source: str = Field(..., description="Git/HTTPS URL or local path to a skill")
    profile: str = STATIC_PROFILE


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/profiles")
def profiles() -> list[dict]:
    """List scan profiles and whether their API key is configured (never the key itself)."""
    env = {**read_dotenv(DOTENV), **os.environ}
    items = [{"id": STATIC_PROFILE, "label": "Static only (no LLM)", "ready": True}]
    labels = {"sonnet": "Claude Sonnet 5.5", "sol-medium": "GPT-6.1 Sol (medium effort)"}
    for name in PROFILES:
        items.append(
            {
                "id": name,
                "label": labels.get(name, name),
                "model": PROFILES[name]["SKILLSPECTOR_MODEL"],
                "ready": bool(env.get(REQUIRED_KEY[name])),
                "key": REQUIRED_KEY[name],
            }
        )
    return items


@app.post("/api/scan")
def scan(request: ScanRequest) -> dict:
    source = request.source.strip()
    if not source:
        raise HTTPException(400, "source is required")
    if not source.startswith(("https://", "http://", "git@")) and not Path(source).exists():
        raise HTTPException(400, "source must be a URL or an existing local path")
    return _submit(source, source, request.profile).summary()


@app.post("/api/scan/upload")
async def scan_upload(
    file: Annotated[UploadFile, File()],
    profile: Annotated[str, Form()] = STATIC_PROFILE,
) -> dict:
    name = Path(file.filename or "skill.zip").name
    if not name.lower().endswith((".zip", ".md")):
        raise HTTPException(400, "upload a .zip archive or a single .md file")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "upload larger than 50 MB")
    upload_dir = Path(tempfile.mkdtemp(prefix="skillui-upload-"))
    target = upload_dir / name
    target.write_bytes(data)
    try:
        return _submit(f"upload: {name}", str(target), profile, cleanup=upload_dir).summary()
    except HTTPException:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise


@app.get("/api/jobs")
def jobs() -> list[dict]:
    with _lock:
        return [job.summary() for job in sorted(_jobs.values(), key=lambda j: -j.started)]


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    found = _jobs.get(job_id)
    if not found:
        raise HTTPException(404, "unknown job")
    return {**found.summary(), "report": found.report}


@app.get("/api/search")
def search(q: str, limit: int = 20) -> list[dict]:
    if not q.strip():
        raise HTTPException(400, "query is required")
    try:
        return github_search.search_skills(q.strip(), limit=limit)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"GitHub search failed: {exc}") from exc


@app.get("/api/repo/{owner}/{name}")
def repo(owner: str, name: str) -> dict:
    try:
        return github_search.repo_details(f"{owner}/{name}")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"GitHub lookup failed: {exc}") from exc


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
