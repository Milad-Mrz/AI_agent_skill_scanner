"""Local FastAPI web UI for scanning agent skills.

Each scan runs the fork review pipeline (``python -m skillspector.ext.review``: upstream
scan, reveal, bundle judge, explain, combined verdict) in a subprocess with the
environment of the selected LLM profile, so the UI never imports untrusted skill content into this
process. The page escapes everything it shows and never renders skill markdown.

Bind to localhost only: the UI accepts local paths and spends API credit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
from skillspector.ext.llm_profiles import (
    PROFILE_LABELS,
    PROFILES,
    profile_env,
    profile_ready,
    read_dotenv,
)

STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[4]
DOTENV = Path(os.environ.get("SKILLSPECTOR_UI_DOTENV", REPO_ROOT / ".env"))
SCAN_TIMEOUT_SECONDS = 900
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
STATIC_PROFILE = "static"
# Scan history survives server restarts and crashes.
DATA_DIR = Path(
    os.environ.get(
        "SKILLSPECTOR_UI_DATA",
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "skill-scanner",
    )
)


@dataclass
class Job:
    id: str
    source: str
    profile: str
    strict: bool = True
    status: str = "queued"  # queued | running | done | error | interrupted
    started: float = field(default_factory=time.time)
    finished: float | None = None
    report: dict | None = None
    error: str | None = None
    cleanup: Path | None = None

    def summary(self) -> dict:
        report = self.report or {}
        risk = report.get("verdict") or report.get("risk_assessment", {})
        return {
            "id": self.id,
            "source": self.source,
            "profile": self.profile,
            "strict": self.strict,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "score": risk.get("score"),
            "recommendation": risk.get("recommendation"),
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2)
_PERSISTED_FIELDS = ("id", "source", "profile", "strict", "status", "started", "finished", "error")

app = FastAPI(title="Skill Scanner", docs_url="/api/docs", redoc_url=None)


def _job_path(job_id: str) -> Path:
    return DATA_DIR / "scans" / f"{job_id}.json"


def _save(job: Job) -> None:
    """Write the job (and its report, when present) atomically to the history directory."""
    path = _job_path(job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {name: getattr(job, name) for name in _PERSISTED_FIELDS}
    record["report"] = job.report
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)


def load_history() -> None:
    """Load saved scans; jobs that were queued or running when the server stopped are marked."""
    folder = DATA_DIR / "scans"
    if not folder.is_dir():
        return
    for path in folder.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            job = Job(**{name: record[name] for name in _PERSISTED_FIELDS if name in record})
        except (OSError, ValueError, TypeError, KeyError):
            continue
        job.report = record.get("report")
        if job.status in {"queued", "running"}:
            job.status = "interrupted"
            job.error = "The server stopped before this scan finished. Run it again."
            _save(job)
        _jobs[job.id] = job


def _scan_env(profile: str, strict: bool) -> dict[str, str]:
    env = dict(os.environ) if profile == STATIC_PROFILE else profile_env(profile, dotenv=DOTENV)
    env["SKILLSPECTOR_STRICT"] = "1" if strict else ""
    return env


def _run_scan(job: Job, target: str) -> None:
    job.status = "running"
    _save(job)
    workdir = Path(tempfile.mkdtemp(prefix="skillui-"))
    report_path = workdir / "report.json"
    cmd = [sys.executable, "-m", "skillspector.ext.review", target, "--out", str(report_path)]
    if job.profile == STATIC_PROFILE:
        cmd.append("--no-llm")
    if not job.strict:
        cmd.append("--no-strict")
    try:
        result = subprocess.run(
            cmd,
            env=_scan_env(job.profile, job.strict),
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
        job.status = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as the job error
        job.error = str(exc)
        job.status = "error"
    finally:
        job.finished = time.time()
        _save(job)
        shutil.rmtree(workdir, ignore_errors=True)
        if job.cleanup:
            shutil.rmtree(job.cleanup, ignore_errors=True)


def _submit(
    source: str, target: str, profile: str, strict: bool = True, cleanup: Path | None = None
) -> Job:
    if profile != STATIC_PROFILE and profile not in PROFILES:
        raise HTTPException(400, f"unknown profile {profile!r}")
    if profile != STATIC_PROFILE:
        try:
            profile_env(profile, dotenv=DOTENV)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    job = Job(
        id=uuid.uuid4().hex[:12], source=source, profile=profile, strict=strict, cleanup=cleanup
    )
    with _lock:
        _jobs[job.id] = job
    _save(job)
    _executor.submit(_run_scan, job, target)
    return job


class ScanRequest(BaseModel):
    source: str = Field(..., description="Git/HTTPS URL or local path to a skill")
    profile: str = STATIC_PROFILE
    strict: bool = True


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/profiles")
def profiles() -> list[dict]:
    """List scan profiles and whether their API key is configured (never the key itself)."""
    env = {**read_dotenv(DOTENV), **os.environ}
    items = [{"id": STATIC_PROFILE, "label": "Static only (no LLM)", "ready": True}]
    for name in PROFILES:
        ready, missing = profile_ready(name, env)
        items.append(
            {
                "id": name,
                "label": PROFILE_LABELS.get(name, name),
                "model": PROFILES[name]["SKILLSPECTOR_MODEL"],
                "provider": PROFILES[name]["SKILLSPECTOR_PROVIDER"],
                "ready": ready,
                "missing": missing,
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
    return _submit(source, source, request.profile, strict=request.strict).summary()


@app.post("/api/scan/upload")
async def scan_upload(
    file: Annotated[UploadFile, File()],
    profile: Annotated[str, Form()] = STATIC_PROFILE,
    strict: Annotated[bool, Form()] = True,
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
        return _submit(
            f"upload: {name}", str(target), profile, strict=strict, cleanup=upload_dir
        ).summary()
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


class RewriteRequest(BaseModel):
    keep: list[int] = Field(..., description="Indexes of explained instructions to keep")


@app.post("/api/jobs/{job_id}/rewrite")
def rewrite_job(job_id: str, request: RewriteRequest) -> dict:
    """Build a clean SKILL.md from the kept plain-English instructions of a finished scan."""
    found = _jobs.get(job_id)
    if not found or found.status != "done" or not found.report:
        raise HTTPException(404, "no finished scan with that id")
    if found.profile == STATIC_PROFILE:
        raise HTTPException(400, "rewrite needs a scan made with an LLM model")
    explanation = found.report.get("explanation") or {}
    items = explanation.get("items") or []
    kept = [items[i] for i in request.keep if 0 <= i < len(items)]
    if not kept:
        raise HTTPException(400, "keep at least one instruction")
    with tempfile.TemporaryDirectory(prefix="skillui-rewrite-") as tmp:
        source, out = Path(tmp) / "in.json", Path(tmp) / "out.json"
        source.write_text(
            json.dumps({"purpose": explanation.get("purpose", ""), "items": kept}),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "skillspector.ext.review",
                "rewrite",
                "--input",
                str(source),
                "--out",
                str(out),
            ],
            env=_scan_env(found.profile, found.strict),
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT_SECONDS,
            check=False,
        )
        if not out.is_file():
            raise HTTPException(502, f"rewrite failed: {result.stderr.strip()[-500:]}")
        rewritten = json.loads(out.read_text(encoding="utf-8"))
    found.report["rewrite"] = rewritten
    _save(found)
    return rewritten


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with _lock:
        found = _jobs.pop(job_id, None)
    if not found:
        raise HTTPException(404, "unknown job")
    _job_path(job_id).unlink(missing_ok=True)
    return {"deleted": job_id}


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


load_history()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
