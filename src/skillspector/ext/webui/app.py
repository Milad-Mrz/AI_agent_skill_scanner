"""Local FastAPI web UI for scanning agent skills, one at a time or in batches.

Batch flow: add repositories (each is fetched once with the upstream hardened clone and
split into its skills), pick operations (static rules, a model review, explain, sandbox,
pin), run. Every (skill, operation) pair becomes a queued task with live stage progress;
more repositories can be added and queued while tasks run.

Every task runs a fork module (review, sandbox) in a subprocess with the environment of
the chosen LLM profile, so this process never imports untrusted skill content. The page
escapes everything it shows and never renders skill markdown.

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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from skillspector.ext import batch as batches
from skillspector.ext import github_search, pricing
from skillspector.ext import pin as pins
from skillspector.ext.llm_profiles import (
    PROFILE_LABELS,
    PROFILES,
    profile_env,
    profile_ready,
    read_dotenv,
)
from skillspector.ext.review import add_dynamic

STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[4]
DOTENV = Path(os.environ.get("SKILLSPECTOR_UI_DOTENV", REPO_ROOT / ".env"))
SCAN_TIMEOUT_SECONDS = int(os.environ.get("SKILLSPECTOR_UI_TASK_TIMEOUT", "2400"))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
STATIC_PROFILE = "static"
PROGRESS_PREFIX = "@@PROGRESS "
# Scan history survives server restarts and crashes.
DATA_DIR = Path(
    os.environ.get(
        "SKILLSPECTOR_UI_DATA",
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "skill-scanner",
    )
)
# LLM tasks share one API key; running them one at a time avoids provider throttling,
# which otherwise shows up as incompletely inspected files.
LLM_WORKERS = int(os.environ.get("SKILLSPECTOR_UI_LLM_WORKERS", "1"))
STATIC_WORKERS = int(os.environ.get("SKILLSPECTOR_UI_STATIC_WORKERS", "3"))

# Stages each task kind reports, in order (for the progress display).
STAGES = {
    "review-static": ["fetch", "upstream-scan", "reveal", "verdict"],
    "review": ["fetch", "upstream-scan", "reveal", "judge", "verdict"],
    "review-full": ["fetch", "upstream-scan", "reveal", "judge", "explain", "verdict"],
    "explain": ["fetch", "upstream-scan", "reveal", "explain", "verdict"],
    "sandbox": ["image", "prepare", "agent-run", "analyse"],
    "pin": ["pin"],
}


@dataclass
class Job:
    id: str
    source: str
    profile: str
    strict: bool = True
    status: str = "queued"  # queued | running | done | error | interrupted | cancelled
    started: float = field(default_factory=time.time)
    finished: float | None = None
    report: dict | None = None
    error: str | None = None
    cleanup: Path | None = None
    kind: str = "review-full"  # key of STAGES
    operation: str = ""
    label: str = ""
    repo_id: str = ""
    repo_name: str = ""
    skill: str = ""
    target: str = ""  # what the subprocess scans (local cache path, URL or upload)
    batch_id: str = ""
    stage: str = ""
    stage_times: dict = field(default_factory=dict)
    began: float | None = None

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
            "began": self.began,
            "error": self.error,
            "score": risk.get("score"),
            "recommendation": risk.get("recommendation"),
            "kind": self.kind,
            "operation": self.operation,
            "label": self.label or PROFILE_LABELS.get(self.profile, self.profile),
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "skill": self.skill,
            "batch_id": self.batch_id,
            "stage": self.stage,
            "stages": STAGES.get(self.kind, []),
            "stage_times": self.stage_times,
            "issues": _issue_keys(risk.get("issues") or report.get("issues") or []),
            "usage": _job_usage(self),
        }


def _job_usage(job: Job) -> dict | None:
    """Tokens and estimated cost of one task (review stages, sandbox agent, rewrites)."""
    report = job.report or {}
    model = PROFILES.get(job.profile, {}).get("SKILLSPECTOR_MODEL")
    provider = PROFILES.get(job.profile, {}).get("SKILLSPECTOR_PROVIDER")
    input_tokens = output_tokens = 0
    if usage := report.get("usage"):
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
    else:
        # Older scans: only the upstream LLM telemetry was kept.
        upstream = report.get("report") if "report" in report else report
        for entry in ((upstream or {}).get("metadata") or {}).get("inference_usage") or []:
            input_tokens += int(entry.get("prompt_tokens") or 0)
            output_tokens += int(entry.get("completion_tokens") or 0)
    events = (report.get("sandbox") or {}).get("events") or {}
    input_tokens += int(events.get("input_tokens") or 0)
    output_tokens += int(events.get("output_tokens") or 0)
    if rewrite_usage := (report.get("rewrite") or {}).get("usage"):
        input_tokens += int(rewrite_usage.get("input_tokens") or 0)
        output_tokens += int(rewrite_usage.get("output_tokens") or 0)
    if not (input_tokens or output_tokens):
        return None
    return {
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": pricing.estimate(model, input_tokens, output_tokens),
    }


def _issue_keys(issues: list[dict]) -> list[list]:
    """Compact [rule, severity, file, line, title] rows so the UI can merge issues per skill."""
    rows = []
    for issue in issues[:300]:
        location = issue.get("location") or {}
        rows.append(
            [
                str(issue.get("id", "")),
                str(issue.get("severity", "")),
                str(location.get("file", "")),
                location.get("start_line"),
                str(issue.get("finding") or issue.get("pattern") or "")[:120],
            ]
        )
    return rows


_jobs: dict[str, Job] = {}
_processes: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()
_llm_pool = ThreadPoolExecutor(max_workers=max(1, LLM_WORKERS))
_static_pool = ThreadPoolExecutor(max_workers=max(1, STATIC_WORKERS))
_fetch_pool = ThreadPoolExecutor(max_workers=2)
_PERSISTED_FIELDS = (
    "id",
    "source",
    "profile",
    "strict",
    "status",
    "started",
    "finished",
    "error",
    "kind",
    "operation",
    "label",
    "repo_id",
    "repo_name",
    "skill",
    "target",
    "batch_id",
    "stage",
    "stage_times",
    "began",
)

app = FastAPI(title="Skill Scanner", docs_url="/api/docs", redoc_url=None)


def _job_path(job_id: str) -> Path:
    return DATA_DIR / "scans" / f"{job_id}.json"


def _save(job: Job) -> None:
    """Write the job (and its report, when present) atomically to the history directory."""
    path = _job_path(job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {name: getattr(job, name) for name in _PERSISTED_FIELDS}
    record["report"] = job.report
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)


def _sandbox_reached_model(report: dict | None) -> bool:
    events = ((report or {}).get("sandbox") or {}).get("events") or {}
    return any(
        e.get("kind") == "llm_call" and e.get("status") == 200
        for e in events.get("gateway_events") or []
    )


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
        if (
            job.status == "done"
            and job.kind == "sandbox"
            and not _sandbox_reached_model(job.report)
        ):
            # Older runs counted a sandbox whose agent never got a model reply as clean.
            job.status = "error"
            job.error = "the agent never got a model reply, so nothing was tested (API failed)"
            _save(job)
        if job.status in {"queued", "running"}:
            job.status = "interrupted"
            job.error = "The server stopped before this task finished. Run it again."
            _save(job)
        _jobs[job.id] = job


def _scan_env(profile: str, strict: bool) -> dict[str, str]:
    env = dict(os.environ) if profile == STATIC_PROFILE else profile_env(profile, dotenv=DOTENV)
    env["SKILLSPECTOR_STRICT"] = "1" if strict else ""
    if profile != STATIC_PROFILE:
        # Small per-skill tasks rarely need it, but large bundles must not stop at 600 s.
        env.setdefault("SKILLSPECTOR_MAX_WORKFLOW_SECONDS", str(SCAN_TIMEOUT_SECONDS - 300))
    return env


def _command(job: Job, out: Path) -> list[str] | None:
    python = [sys.executable, "-m"]
    if job.kind == "sandbox":
        return python + [
            "skillspector.ext.sandbox",
            job.target,
            "--profile",
            job.profile,
            "--dotenv",
            str(DOTENV),
            "--out",
            str(out),
        ]
    if job.kind == "pin":
        return None
    cmd = python + ["skillspector.ext.review", job.target, "--out", str(out)]
    if job.profile == STATIC_PROFILE or job.kind == "review-static":
        cmd.append("--no-llm")
    elif job.kind == "review":
        cmd.append("--no-explain")
    elif job.kind == "explain":
        cmd += ["--no-judge", "--static-upstream"]
    if not job.strict:
        cmd.append("--no-strict")
    return cmd


def _set_stage(job: Job, stage: str) -> None:
    job.stage = stage
    job.stage_times[stage] = round(time.time(), 1)


def _run_job(job: Job) -> None:
    if job.status == "cancelled":
        return
    job.status = "running"
    job.began = time.time()
    _save(job)
    workdir = Path(tempfile.mkdtemp(prefix="skillui-"))
    out = workdir / "result.json"
    try:
        if job.kind == "pin":
            _set_stage(job, "pin")
            record = pins.pin(job.source, batches.repo_name(job.source).replace("/", "-"))
            job.report = {
                "verdict": {
                    "score": None,
                    "recommendation": None,
                    "reasons": [
                        f"pinned {len(record.files)} files, commit {record.commit or 'n/a'}"
                    ],
                    "issues": [],
                },
                "pin": {k: v for k, v in record.to_dict().items() if k != "files"},
            }
        else:
            process = subprocess.Popen(
                _command(job, out),
                env=_scan_env(job.profile, job.strict),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _processes[job.id] = process
            timer = threading.Timer(SCAN_TIMEOUT_SECONDS, process.kill)
            timer.start()
            stderr_tail: list[str] = []
            reader = threading.Thread(
                target=lambda: stderr_tail.extend(process.stderr.read().splitlines()[-40:]),
                daemon=True,
            )
            reader.start()
            try:
                for line in process.stdout:
                    if line.startswith(PROGRESS_PREFIX):
                        try:
                            _set_stage(job, json.loads(line[len(PROGRESS_PREFIX) :])["stage"])
                        except (ValueError, KeyError):
                            pass
                process.wait()
            finally:
                timer.cancel()
                reader.join(timeout=5)
                _processes.pop(job.id, None)
            if job.status == "cancelled":
                return
            if not out.is_file():
                timed_out = process.returncode in (-9, 137)
                raise RuntimeError(
                    f"timed out after {SCAN_TIMEOUT_SECONDS} s in stage {job.stage!r}"
                    if timed_out
                    else f"exit {process.returncode}: {' '.join(stderr_tail)[-800:]}"
                )
            data = json.loads(out.read_text(encoding="utf-8"))
            if job.kind == "sandbox":
                if data.get("error"):
                    raise RuntimeError(f"sandbox: {data['error']}")
                base = {
                    "report": {},
                    "verdict": {"score": 0, "recommendation": "SAFE", "reasons": [], "issues": []},
                }
                data = add_dynamic(base, data)
            job.report = data
        job.status = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as the task error
        job.error = str(exc)
        job.status = "error" if job.status != "cancelled" else "cancelled"
    finally:
        job.finished = time.time()
        _save(job)
        shutil.rmtree(workdir, ignore_errors=True)
        if job.cleanup:
            shutil.rmtree(job.cleanup, ignore_errors=True)


def _enqueue(job: Job) -> Job:
    with _lock:
        _jobs[job.id] = job
    _save(job)
    pool = _static_pool if job.profile == STATIC_PROFILE or job.kind == "pin" else _llm_pool
    pool.submit(_run_job, job)
    return job


def _check_profile(profile: str) -> None:
    if profile == STATIC_PROFILE:
        return
    if profile not in PROFILES:
        raise HTTPException(400, f"unknown profile {profile!r}")
    try:
        profile_env(profile, dotenv=DOTENV)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if failure := _probe_failure(profile):
        raise HTTPException(
            400, f"{PROFILE_LABELS.get(profile, profile)}: {failure}; fix the key and re-test"
        )


def _submit(
    source: str, target: str, profile: str, strict: bool = True, cleanup: Path | None = None
) -> Job:
    _check_profile(profile)
    return _enqueue(
        Job(
            id=uuid.uuid4().hex[:12],
            source=source,
            profile=profile,
            strict=strict,
            cleanup=cleanup,
            target=target,
            kind="review-static" if profile == STATIC_PROFILE else "review-full",
            operation=profile,
            repo_name=batches.repo_name(source),
        )
    )


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


@app.post("/api/jobs/{job_id}/sandbox")
def sandbox_job(job_id: str) -> dict:
    """Run the scanned skill with a real agent in the Docker sandbox (dynamic analysis)."""
    found = _jobs.get(job_id)
    if not found or found.status != "done" or not found.report:
        raise HTTPException(404, "no finished scan with that id")
    if found.profile == STATIC_PROFILE:
        raise HTTPException(400, "the sandbox agent needs an LLM model; rescan with one")
    if found.source.startswith("upload: "):
        raise HTTPException(400, "uploads cannot be re-fetched; scan a URL or local path")
    with tempfile.TemporaryDirectory(prefix="skillui-sandbox-") as tmp:
        out = Path(tmp) / "dyn.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "skillspector.ext.sandbox",
                found.target or found.source,
                "--profile",
                found.profile,
                "--dotenv",
                str(DOTENV),
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
            raise HTTPException(502, f"sandbox failed: {result.stderr.strip()[-500:]}")
        dynamic = json.loads(out.read_text(encoding="utf-8"))
    if dynamic.get("error"):
        raise HTTPException(502, f"sandbox failed: {dynamic['error']}")
    add_dynamic(found.report, dynamic)
    _save(found)
    return {**found.summary(), "report": found.report}


@app.post("/api/jobs/{job_id}/pin")
def pin_job(job_id: str) -> dict:
    """Pin the exact files of a finished scan's source, with its verdict."""
    found = _jobs.get(job_id)
    if not found or found.status != "done" or not found.report:
        raise HTTPException(404, "no finished scan with that id")
    if found.source.startswith("upload: "):
        raise HTTPException(400, "uploads cannot be re-fetched; pin a URL or local path")
    verdict = found.report.get("verdict") or found.report.get("risk_assessment") or {}
    try:
        record = pins.pin(
            found.source,
            verdict={k: verdict.get(k) for k in ("score", "recommendation")},
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(502, f"pin failed: {exc}") from exc
    return {**record.to_dict(), "file_count": len(record.files)}


@app.get("/api/pins")
def list_pins() -> list[dict]:
    return [
        {**{k: v for k, v in p.to_dict().items() if k != "files"}, "file_count": len(p.files)}
        for p in pins.list_pins()
    ]


@app.post("/api/pins/{name}/verify")
def verify_pin(name: str) -> dict:
    try:
        return pins.verify(name).to_dict()
    except FileNotFoundError as exc:
        raise HTTPException(404, "unknown pin") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(502, f"verify failed: {exc}") from exc


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with _lock:
        found = _jobs.pop(job_id, None)
    if not found:
        raise HTTPException(404, "unknown job")
    if found.status in {"queued", "running"}:
        found.status = "cancelled"
        if process := _processes.get(job_id):
            process.kill()
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


# ---------------------------------------------------------------------------
# Batch: repositories, operations, runs
# ---------------------------------------------------------------------------


@dataclass
class Repo:
    id: str
    source: str
    name: str
    status: str = "fetching"  # fetching | ready | error
    skills: list[dict] = field(default_factory=list)
    root: str = ""
    error: str | None = None
    added: float = field(default_factory=time.time)
    truncated: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("root")
        return data


_repos: dict[str, Repo] = {}


def _repos_path() -> Path:
    return DATA_DIR / "repos.json"


def _save_repos() -> None:
    _repos_path().parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        data = [asdict(r) for r in _repos.values()]
    tmp = _repos_path().with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(_repos_path())


def load_repos() -> None:
    if not _repos_path().is_file():
        return
    try:
        for record in json.loads(_repos_path().read_text(encoding="utf-8")):
            repo_record = Repo(**record)
            if repo_record.status == "fetching" or not Path(repo_record.root).is_dir():
                repo_record.status, repo_record.error = "error", "fetch interrupted; refresh it"
            _repos[repo_record.id] = repo_record
    except (OSError, ValueError, TypeError):
        return


def _fetch_repo(repo_record: Repo) -> None:
    try:
        fetched = batches.fetch(repo_record.source, DATA_DIR / "repo-cache")
        repo_record.root = str(fetched.root)
        repo_record.skills = [s.to_dict() for s in fetched.skills]
        repo_record.truncated = fetched.truncated
        repo_record.status = "ready" if fetched.skills else "error"
        repo_record.error = None if fetched.skills else "no SKILL.md found in this repository"
    except Exception as exc:  # noqa: BLE001 - shown on the tile
        repo_record.status, repo_record.error = "error", str(exc)[:500]
    _save_repos()


class RepoRequest(BaseModel):
    sources: list[str] = Field(..., description="GitHub/HTTPS URLs or local paths")


@app.post("/api/repos")
def add_repos(request: RepoRequest) -> list[dict]:
    added = []
    for raw in request.sources:
        source = raw.strip()
        if not source:
            continue
        if not source.startswith(("https://", "http://")) and not Path(source).exists():
            raise HTTPException(400, f"not a URL or existing path: {source[:200]}")
        existing = next((r for r in _repos.values() if r.source == source), None)
        if existing:
            added.append(existing.to_dict())
            continue
        repo_record = Repo(uuid.uuid4().hex[:10], source, batches.repo_name(source))
        with _lock:
            _repos[repo_record.id] = repo_record
        _fetch_pool.submit(_fetch_repo, repo_record)
        added.append(repo_record.to_dict())
    _save_repos()
    return added


@app.get("/api/repos")
def list_repos() -> list[dict]:
    with _lock:
        return [r.to_dict() for r in sorted(_repos.values(), key=lambda r: r.added)]


@app.post("/api/repos/{repo_id}/refresh")
def refresh_repo(repo_id: str) -> dict:
    repo_record = _repos.get(repo_id)
    if not repo_record:
        raise HTTPException(404, "unknown repository")
    repo_record.status, repo_record.error = "fetching", None
    _fetch_pool.submit(_fetch_repo, repo_record)
    return repo_record.to_dict()


@app.delete("/api/repos/{repo_id}")
def remove_repo(repo_id: str) -> dict:
    with _lock:
        repo_record = _repos.pop(repo_id, None)
    if not repo_record:
        raise HTTPException(404, "unknown repository")
    if repo_record.root:
        shutil.rmtree(repo_record.root, ignore_errors=True)
    _save_repos()
    return {"deleted": repo_id}


def operations_catalog() -> list[dict]:
    """Operation tiles: static rules, one review per model, explain, sandbox, pin."""
    env = {**read_dotenv(DOTENV), **os.environ}
    ops = [
        {
            "id": "static",
            "label": "SkillSpector-MM",
            "sub": "static rules + reveal · free",
            "icon": "shield",
            "kind": "review-static",
            "ready": True,
            "needs_model": False,
        }
    ]
    for name in PROFILES:
        ready, missing = profile_ready(name, env)
        if ready and (failure := _probe_failure(name)):
            ready, missing = False, f"a working key ({failure})"
        provider = PROFILES[name]["SKILLSPECTOR_PROVIDER"]
        ops.append(
            {
                "id": f"model:{name}",
                "label": PROFILE_LABELS.get(name, name).split(" (")[0],
                "sub": {
                    "anthropic": "hosted",
                    "openai": "hosted",
                    "nv_build": "open · hosted",
                    "ollama": "open · local",
                }.get(provider, provider)
                + " · judge",
                "icon": {"ollama": "chip", "nv_build": "cloud"}.get(provider, "spark"),
                "kind": "review",
                "profile": name,
                "ready": ready,
                "missing": missing,
                "needs_model": False,
            }
        )
    ops += [
        {
            "id": "explain",
            "label": "Explain",
            "sub": "plain English + coverage · slow",
            "icon": "list",
            "kind": "explain",
            "ready": True,
            "needs_model": True,
        },
        {
            "id": "sandbox",
            "label": "Sandbox",
            "sub": "real agent, fake secrets",
            "icon": "box",
            "kind": "sandbox",
            "ready": shutil.which("docker") is not None,
            "missing": None if shutil.which("docker") else "docker",
            "needs_model": True,
        },
        {
            "id": "pin",
            "label": "Pin",
            "sub": "hash files for later verify",
            "icon": "pin",
            "kind": "pin",
            "ready": True,
            "needs_model": False,
        },
    ]
    return ops


# Results of the last real request per profile, keyed by a fingerprint of the key
# so editing .env invalidates them.
_probe_cache: dict[str, dict] = {}
_PROBE_MAX_AGE = 600.0
KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NVIDIA_INFERENCE_KEY")


def _env() -> dict[str, str]:
    return {**read_dotenv(DOTENV), **os.environ}


def _fingerprint(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _mask(value: str) -> str:
    if not value:
        return ""
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "…" * 3


def _probe_all(force: bool = False) -> list[dict]:
    """One tiny real request per keyed profile, cached; re-run when stale or forced."""
    from skillspector.ext.llm_profiles import REQUIRED_KEY, probe_profile

    env = _env()
    todo, results = [], []
    for name in PROFILES:
        key_var = REQUIRED_KEY.get(name)
        if not key_var or not env.get(key_var):
            continue
        cache_key = f"{name}:{_fingerprint(env[key_var])}"
        cached = _probe_cache.get(cache_key)
        if cached and not force and time.time() - cached["tested_at"] < _PROBE_MAX_AGE:
            results.append(cached)
        else:
            todo.append((name, cache_key))
    if todo:
        with ThreadPoolExecutor(max_workers=6) as pool:
            fresh = list(pool.map(lambda item: probe_profile(item[0], env), todo))
        for (name, cache_key), result in zip(todo, fresh, strict=True):
            result.update(
                label=PROFILE_LABELS.get(name, name),
                tested_at=time.time(),
                key_var=REQUIRED_KEY.get(name),
            )
            for stale in [k for k in _probe_cache if k.startswith(f"{name}:")]:
                _probe_cache.pop(stale)
            _probe_cache[cache_key] = result
            results.append(result)
    return sorted(results, key=lambda r: list(PROFILES).index(r["profile"]))


def _probe_failure(name: str) -> str | None:
    """Reason a profile failed its last real request with the current key, if it did."""
    from skillspector.ext.llm_profiles import REQUIRED_KEY

    key_var = REQUIRED_KEY.get(name)
    value = _env().get(key_var or "", "")
    result = _probe_cache.get(f"{name}:{_fingerprint(value)}") if value else None
    if result and result.get("ok") is False:
        return result["detail"].split(":")[0]
    return None


@app.post("/api/models/test")
def test_models() -> list[dict]:
    """One tiny real request per model that has a key (always fresh)."""
    return _probe_all(force=True)


@app.get("/api/keys")
def api_keys(force: bool = False) -> dict:
    """Every API key in the environment (masked) with the live status of the models using it."""
    from skillspector.ext.llm_profiles import REQUIRED_KEY

    env = _env()
    probes = _probe_all(force=force)
    keys = []
    for var in KEY_VARS:
        users = [n for n in PROFILES if REQUIRED_KEY.get(n) == var]
        models = [p for p in probes if p["key_var"] == var]
        if not env.get(var):
            status = "missing"
        elif models and all(m["ok"] for m in models):
            status = "ok"
        elif any(m["ok"] for m in models):
            status = "partial"
        else:
            status = "failed"
        keys.append(
            {
                "var": var,
                "present": bool(env.get(var)),
                "masked": _mask(env.get(var, "")),
                "status": status,
                "used_by": [PROFILE_LABELS.get(n, n).split(" (")[0] for n in users],
                "models": models,
            }
        )
    tested = [p["tested_at"] for p in probes]
    return {"keys": keys, "tested_at": max(tested) if tested else None}


@app.get("/api/operations")
def operations() -> list[dict]:
    return operations_catalog()


class BatchRequest(BaseModel):
    repos: list[str]
    operations: list[str]
    strict: bool = True
    helper_profile: str | None = Field(
        default=None, description="model for Explain and Sandbox; default: first selected model"
    )
    skills: dict[str, list[str]] | None = Field(
        default=None, description="optional {repo_id: [skill paths]}; default all skills"
    )


@app.post("/api/batch")
def run_batch(request: BatchRequest) -> dict:
    catalog = {op["id"]: op for op in operations_catalog()}
    chosen = [catalog[o] for o in request.operations if o in catalog]
    if not chosen:
        raise HTTPException(400, "select at least one operation")
    not_ready = [op["label"] for op in chosen if not op["ready"]]
    if not_ready:
        raise HTTPException(400, f"not available: {', '.join(not_ready)}")
    model_ops = [op for op in chosen if op["kind"] == "review"]
    helper_profile = request.helper_profile or (model_ops[0]["profile"] if model_ops else None)
    if any(op["needs_model"] for op in chosen):
        if helper_profile is None:
            raise HTTPException(400, "Explain and Sandbox need a model: pick one next to Run")
        helper_op = catalog.get(f"model:{helper_profile}")
        if not helper_op or not helper_op["ready"]:
            missing = (helper_op or {}).get("missing") or "an unknown profile"
            raise HTTPException(400, f"the Explain/Sandbox model is not available: needs {missing}")
    batch_id = uuid.uuid4().hex[:8]
    created: list[dict] = []
    for repo_id in request.repos:
        repo_record = _repos.get(repo_id)
        if not repo_record or repo_record.status != "ready":
            continue
        wanted = (request.skills or {}).get(repo_id)
        skills = [s for s in repo_record.skills if not wanted or s["path"] in wanted]
        for op in chosen:
            if op["kind"] == "pin":
                job = Job(
                    uuid.uuid4().hex[:12],
                    repo_record.source,
                    STATIC_PROFILE,
                    request.strict,
                    kind="pin",
                    operation=op["id"],
                    label=op["label"],
                    repo_id=repo_id,
                    repo_name=repo_record.name,
                    skill="(whole repo)",
                    batch_id=batch_id,
                )
                created.append(_enqueue(job).summary())
                continue
            profile = op.get("profile") or (helper_profile if op["needs_model"] else STATIC_PROFILE)
            label = (
                op["label"]
                if not op["needs_model"]
                else (f"{op['label']} · {PROFILE_LABELS.get(profile, profile).split(' (')[0]}")
            )
            for skill in skills:
                target = str(Path(repo_record.root) / skill["path"])
                job = Job(
                    uuid.uuid4().hex[:12],
                    repo_record.source,
                    profile,
                    request.strict,
                    kind=op["kind"],
                    operation=op["id"],
                    label=label,
                    repo_id=repo_id,
                    repo_name=repo_record.name,
                    skill=skill["name"],
                    target=target,
                    batch_id=batch_id,
                )
                created.append(_enqueue(job).summary())
    if not created:
        raise HTTPException(400, "no ready repositories selected")
    return {"batch_id": batch_id, "tasks": created}


def _delete_jobs(selected: list[Job]) -> int:
    for found in selected:
        if found.status in {"queued", "running"}:
            found.status = "cancelled"
            if process := _processes.get(found.id):
                process.kill()
        with _lock:
            _jobs.pop(found.id, None)
        _job_path(found.id).unlink(missing_ok=True)
    return len(selected)


@app.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: str, repo_id: str | None = None, skill: str | None = None) -> dict:
    """Delete a whole run, or one item of it (repo_id + skill); running tasks are stopped."""
    selected = [
        j
        for j in list(_jobs.values())
        if (j.batch_id == batch_id or (batch_id.startswith("single-") and j.id == batch_id[7:]))
        and (repo_id is None or j.repo_id == repo_id)
        and (skill is None or j.skill == skill)
    ]
    if not selected:
        raise HTTPException(404, "nothing to delete")
    return {"deleted": _delete_jobs(selected)}


def _stop(selected: list[Job]) -> int:
    stopped = 0
    for found in selected:
        if found.status in {"queued", "running"}:
            found.status = "cancelled"
            found.error = "stopped by the user"
            if process := _processes.get(found.id):
                process.kill()
            found.finished = time.time()
            _save(found)
            stopped += 1
    return stopped


_SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
_REC_RANK = {"DO_NOT_INSTALL": 0, "CAUTION": 1, "SAFE": 2}


_LLM_KINDS = {"review", "review-full"}


def effective_recommendation(runs: list[Job]) -> str | None:
    """One verdict for a skill from its latest runs.

    Static, Explain and Sandbox runs must each pass (their worst result counts), but
    LLM model reviews are alternatives: passing one API model is enough, so the best
    LLM verdict counts and a dissenting second model does not block the skill.
    """
    groups: dict[str, list[str]] = {}
    for found in runs:
        rec = found.summary().get("recommendation")
        if rec:
            groups.setdefault("llm" if found.kind in _LLM_KINDS else found.kind, []).append(rec)
    picked = [
        max(recs, key=lambda r: _REC_RANK.get(r, -1))
        if group == "llm"
        else min(recs, key=lambda r: _REC_RANK.get(r, 3))
        for group, recs in groups.items()
    ]
    return min(picked, key=lambda r: _REC_RANK.get(r, 3)) if picked else None


def _skill_of(job: Job) -> str:
    return job.skill or job.repo_name or job.source


def combined_results(source: str, skill: str | None = None) -> dict:
    """Latest finished result of every method for one link (optionally one skill), merged."""
    done = [j for j in _jobs.values() if j.source == source and j.status == "done" and j.report]
    if skill is not None:
        done = [j for j in done if _skill_of(j) == skill]
    latest: dict[tuple[str, str], Job] = {}
    for found in sorted(done, key=lambda j: j.finished or j.started):
        latest[(_skill_of(found), found.operation or found.profile)] = found
    skills: dict[str, list[Job]] = {}
    for (name, _op), found in latest.items():
        skills.setdefault(name, []).append(found)

    result = []
    # Repository-level entries (pins) go after the skills.
    for name, runs in sorted(skills.items(), key=lambda kv: (kv[0].startswith("("), kv[0])):
        runs.sort(key=lambda j: j.label or j.operation)
        merged: dict[str, dict] = {}
        for found in runs:
            report = found.report or {}
            issues = (report.get("verdict") or {}).get("issues") or report.get("issues") or []
            for issue in issues:
                location = issue.get("location") or {}
                key = f"{issue.get('id')}|{location.get('file')}|{location.get('start_line')}"
                entry = merged.setdefault(key, {**issue, "found_by": []})
                method = found.label or found.operation
                if method not in entry["found_by"]:
                    entry["found_by"].append(method)
                if not entry.get("code_snippet") and issue.get("code_snippet"):
                    entry["code_snippet"] = issue["code_snippet"]

        def latest_with(field_name: str, runs: list[Job] = runs) -> tuple[Job | None, object]:
            best = None
            for found in runs:
                if (found.report or {}).get(field_name):
                    if best is None or (found.finished or 0) > (best.finished or 0):
                        best = found
            return best, (best.report or {}).get(field_name) if best else None

        explain_job, explanation = latest_with("explanation")
        _sandbox_job, sandbox = latest_with("sandbox")
        _reveal_job, reveal = latest_with("reveal")
        _judge_job, judge = latest_with("judge")
        result.append(
            {
                "skill": name,
                "methods": [j.summary() for j in runs],
                "recommendation": effective_recommendation(runs),
                "issues": sorted(
                    merged.values(),
                    key=lambda i: (_SEV_RANK.get(i.get("severity"), 5), -len(i["found_by"])),
                ),
                "reveal": reveal or [],
                "explanation": explanation,
                "explain_job": explain_job.id if explain_job else None,
                "judge": judge,
                "sandbox": sandbox,
            }
        )
    return {"source": source, "name": batches.repo_name(source), "skills": result}


@app.get("/api/combined")
def combined(source: str, skill: str | None = None) -> dict:
    data = combined_results(source, skill)
    if not data["skills"]:
        raise HTTPException(404, "no finished results for that link")
    return data


@app.delete("/api/history")
def delete_history(source: str) -> dict:
    """Delete every task for one link (stopping unfinished ones)."""
    selected = [j for j in list(_jobs.values()) if j.source == source]
    if not selected:
        raise HTTPException(404, "nothing to delete")
    return {"deleted": _delete_jobs(selected)}


@app.get("/api/usage")
def usage() -> dict:
    """Token use and estimated cost per API model, over all saved tasks."""
    by_model: dict[str, dict] = {}
    for found in list(_jobs.values()):
        task_usage = _job_usage(found)
        if not task_usage:
            continue
        key = task_usage["model"] or found.profile
        slot = by_model.setdefault(
            key,
            {
                "model": key,
                "provider": task_usage["provider"],
                "tasks": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "priced": True,
            },
        )
        slot["tasks"] += 1
        slot["input_tokens"] += task_usage["input_tokens"]
        slot["output_tokens"] += task_usage["output_tokens"]
        if task_usage["cost_usd"] is None:
            slot["priced"] = False
        else:
            slot["cost_usd"] = round(slot["cost_usd"] + task_usage["cost_usd"], 4)
    models = sorted(by_model.values(), key=lambda m: -m["cost_usd"])
    return {
        "models": models,
        "total_cost_usd": round(sum(m["cost_usd"] for m in models), 4),
        "pricing_file": str(pricing.PRICING_FILE),
        "prices": {k: list(v) for k, v in pricing.prices().items()},
    }


def _clone_job(found: Job) -> Job:
    return Job(
        uuid.uuid4().hex[:12],
        found.source,
        found.profile,
        found.strict,
        kind=found.kind,
        operation=found.operation,
        label=found.label,
        repo_id=found.repo_id,
        repo_name=found.repo_name,
        skill=found.skill,
        target=found.target,
        batch_id=found.batch_id,
    )


@app.post("/api/jobs/{job_id}/redo")
def redo_job(job_id: str) -> dict:
    """Queue the same operation on the same skill again."""
    found = _jobs.get(job_id)
    if not found:
        raise HTTPException(404, "unknown job")
    if found.kind != "pin" and found.target and not Path(found.target).exists():
        raise HTTPException(400, "the fetched copy is gone; refresh the repository first")
    _check_profile(found.profile)
    return _enqueue(_clone_job(found)).summary()


@app.post("/api/redo-failed")
def redo_failed(source: str) -> dict:
    """Queue again every skill/operation of a link whose latest task failed or was stopped."""
    latest: dict[tuple[str, str], Job] = {}
    for found in sorted(_jobs.values(), key=lambda j: j.started):
        if found.source == source:
            latest[(_skill_of(found), found.operation)] = found
    redo = [j for j in latest.values() if j.status in {"error", "cancelled", "interrupted"}]
    return {"queued": [_enqueue(_clone_job(j)).summary() for j in redo]}


@app.post("/api/stop-all")
def stop_all() -> dict:
    """Stop every running task and empty the waiting list."""
    return {"stopped": _stop(list(_jobs.values()))}


@app.post("/api/batches/{batch_id}/stop")
def stop_batch(batch_id: str) -> dict:
    return {"stopped": _stop([j for j in list(_jobs.values()) if j.batch_id == batch_id])}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    found = _jobs.get(job_id)
    if not found:
        raise HTTPException(404, "unknown job")
    if found.status in {"queued", "running"}:
        found.status = "cancelled"
        found.error = "cancelled by the user"
        if process := _processes.get(job_id):
            process.kill()
        found.finished = time.time()
        _save(found)
    return found.summary()


# ---------------------------------------------------------------------------
# Skill library: keep / delete decisions per skill, cleaned repositories, export
# ---------------------------------------------------------------------------

_library: dict[str, dict[str, str]] = {}
# A repository can be saved only when every remaining skill passed all of these.
REQUIRED_METHODS = ("static", "model", "explain", "sandbox")
_METHOD_KINDS = {
    "review-static": {"static"},
    "review": {"static", "model"},  # model reviews include the static rules
    "review-full": {"static", "model", "explain"},
    "explain": {"explain"},
    "sandbox": {"sandbox"},
}  # repo_id -> {skill path: "deleted" | "kept"}


def _library_path() -> Path:
    return DATA_DIR / "library.json"


def _save_library() -> None:
    _library_path().parent.mkdir(parents=True, exist_ok=True)
    _library_path().write_text(json.dumps(_library), encoding="utf-8")


def load_library() -> None:
    try:
        _library.update(json.loads(_library_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def _latest_by_skill(source: str) -> dict[str, list[Job]]:
    latest: dict[tuple[str, str], Job] = {}
    for found in sorted(_jobs.values(), key=lambda j: j.finished or j.started):
        if found.source == source and found.status == "done" and found.kind != "pin":
            latest[(_skill_of(found), found.operation)] = found
    grouped: dict[str, list[Job]] = {}
    for (name, _op), found in latest.items():
        grouped.setdefault(name, []).append(found)
    return grouped


def _passed_methods(found: Job) -> set[str]:
    """Methods a finished run really completed (a stage that errored does not count)."""
    report = found.report or {}
    methods = set(_METHOD_KINDS.get(found.kind, set()))
    judge = report.get("judge")
    if "model" in methods and (not judge or (judge.get("error") and not judge.get("findings"))):
        methods.discard("model")
    explanation = report.get("explanation")
    if "explain" in methods and not (explanation and explanation.get("items")):
        methods.discard("explain")
    if "sandbox" in methods and not _sandbox_reached_model(report):
        methods.discard("sandbox")
    return methods


def library_view() -> list[dict]:
    out = []
    for repo_record in sorted(_repos.values(), key=lambda r: r.added):
        if repo_record.status != "ready":
            continue
        states = _library.get(repo_record.id, {})
        results = _latest_by_skill(repo_record.source)
        skills = []
        for skill in repo_record.skills:
            runs = results.get(skill["name"], [])
            merged: dict[str, str] = {}
            for found in runs:
                for rule, sev, file, line, _title in found.summary()["issues"]:
                    key = f"{rule}|{file}|{line}"
                    if (
                        _SEV_RANK.get(sev, 9) < _SEV_RANK.get(merged.get(key, "INFO"), 9)
                        or key not in merged
                    ):
                        merged[key] = sev
            counts: dict[str, int] = {}
            for sev in merged.values():
                counts[sev] = counts.get(sev, 0) + 1
            done_kinds: set[str] = set()
            for found in runs:
                done_kinds |= _passed_methods(found)
            skills.append(
                {
                    "path": skill["path"],
                    "name": skill["name"],
                    "checks": {m: m in done_kinds for m in REQUIRED_METHODS},
                    "state": states.get(skill["path"], "undecided"),
                    "recommendation": effective_recommendation(runs),
                    "methods": [
                        {
                            "label": j.label or j.operation,
                            "score": j.summary()["score"],
                            "recommendation": j.summary()["recommendation"],
                            "id": j.id,
                        }
                        for j in sorted(runs, key=lambda j: j.label or j.operation)
                    ],
                    "counts": counts,
                }
            )
        remaining = [s for s in skills if s["state"] != "deleted"]
        cleaned = bool(remaining) and all(s["recommendation"] == "SAFE" for s in remaining)
        fully_checked = bool(remaining) and all(all(s["checks"].values()) for s in remaining)
        out.append(
            {
                "id": repo_record.id,
                "source": repo_record.source,
                "name": repo_record.name,
                "skills": skills,
                "remaining": len(remaining),
                "deleted": len(skills) - len(remaining),
                "cleaned": cleaned,
                "fully_checked": fully_checked,
                "ready_to_save": cleaned and fully_checked,
                "unscanned": sum(1 for s in remaining if not s["recommendation"]),
            }
        )
    return out


@app.get("/api/library")
def library() -> list[dict]:
    return library_view()


class LibraryAction(BaseModel):
    action: str = Field(..., description="delete | restore | keep")
    mode: str = Field("selected", description="selected | red | red_yellow | all")
    skills: list[str] = Field(default_factory=list, description="skill paths for mode=selected")


@app.post("/api/library/{repo_id}")
def library_action(repo_id: str, request: LibraryAction) -> dict:
    """Mark skills deleted, restored or kept; group modes pick skills by their verdict."""
    view = next((r for r in library_view() if r["id"] == repo_id), None)
    if view is None:
        raise HTTPException(404, "unknown or not-ready repository")
    if request.action not in {"delete", "restore", "keep"}:
        raise HTTPException(400, "action must be delete, restore or keep")
    pick = {
        "red": lambda s: s["recommendation"] == "DO_NOT_INSTALL",
        "red_yellow": lambda s: s["recommendation"] in {"DO_NOT_INSTALL", "CAUTION"},
        "all": lambda s: True,
        "selected": lambda s: s["path"] in set(request.skills),
    }.get(request.mode)
    if pick is None:
        raise HTTPException(400, "unknown mode")
    chosen = [s["path"] for s in view["skills"] if pick(s)]
    states = _library.setdefault(repo_id, {})
    for path in chosen:
        if request.action == "restore":
            states.pop(path, None)
        else:
            states[path] = "deleted" if request.action == "delete" else "kept"
    _save_library()
    return {"changed": len(chosen), "repo": next(r for r in library_view() if r["id"] == repo_id)}


@app.get("/api/library/{repo_id}/export")
def library_export(repo_id: str) -> FileResponse:
    """Zip of the repository's remaining (not deleted) skills plus a verdict manifest."""
    repo_record = _repos.get(repo_id)
    view = next((r for r in library_view() if r["id"] == repo_id), None)
    if not repo_record or not view or not Path(repo_record.root).is_dir():
        raise HTTPException(404, "repository copy not available; refresh it")
    work = Path(tempfile.mkdtemp(prefix="skillui-export-"))
    bundle = work / view["name"].replace("/", "-")
    manifest = []
    for skill in view["skills"]:
        if skill["state"] == "deleted":
            continue
        source_dir = Path(repo_record.root) / skill["path"]
        target = bundle / skill["path"] if skill["path"] != "." else bundle
        shutil.copytree(
            source_dir,
            target,
            symlinks=False,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git"),
        )
        manifest.append({k: skill[k] for k in ("name", "path", "recommendation", "methods")})
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "SKILL_REVIEW.json").write_text(
        json.dumps(
            {
                "source": view["source"],
                "cleaned": view["cleaned"],
                "skills": manifest,
                "removed": [s["path"] for s in view["skills"] if s["state"] == "deleted"],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    archive = shutil.make_archive(
        str(work / "export"), "zip", root_dir=bundle.parent, base_dir=bundle.name
    )
    return FileResponse(
        archive, filename=f"{bundle.name}-reviewed.zip", media_type="application/zip"
    )


# ---------------------------------------------------------------------------
# Saved skills: permanent store of fully cleaned repositories
# ---------------------------------------------------------------------------


def _saved_dir() -> Path:
    return DATA_DIR / "saved"


def _saved_index() -> list[dict]:
    try:
        return json.loads((_saved_dir() / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _write_saved_index(entries: list[dict]) -> None:
    _saved_dir().mkdir(parents=True, exist_ok=True)
    (_saved_dir() / "index.json").write_text(json.dumps(entries, indent=1), encoding="utf-8")


def _not_ready_reasons(view: dict) -> list[str]:
    reasons = []
    for skill in view["skills"]:
        if skill["state"] == "deleted":
            continue
        missing = [m for m, ok in skill["checks"].items() if not ok]
        if missing:
            reasons.append(f"{skill['name']}: missing {', '.join(missing)}")
        elif skill["recommendation"] != "SAFE":
            reasons.append(f"{skill['name']}: {skill['recommendation'] or 'not scanned'}")
    return reasons


@app.post("/api/library/{repo_id}/save")
def save_repo(repo_id: str) -> dict:
    """Move a fully cleaned repository's skills into Saved skills and out of Scan/Library."""
    repo_record = _repos.get(repo_id)
    view = next((r for r in library_view() if r["id"] == repo_id), None)
    if not repo_record or not view:
        raise HTTPException(404, "unknown or not-ready repository")
    if not view["ready_to_save"]:
        raise HTTPException(400, "not cleaned yet: " + "; ".join(_not_ready_reasons(view)[:8]))
    entry_id = uuid.uuid4().hex[:10]
    target = _saved_dir() / entry_id
    kept = [s for s in view["skills"] if s["state"] != "deleted"]
    for skill in kept:
        source_dir = Path(repo_record.root) / skill["path"]
        relative = skill["path"] if skill["path"] != "." else skill["name"]
        shutil.copytree(
            source_dir,
            target / "skills" / relative,
            symlinks=False,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git"),
        )
    entry = {
        "id": entry_id,
        "source": repo_record.source,
        "name": repo_record.name,
        "saved_at": time.time(),
        "commit": pins.read_commit(Path(repo_record.root)),
        "skills": [
            {k: s[k] for k in ("name", "path", "recommendation", "methods", "checks")} for s in kept
        ],
        "removed": [s["path"] for s in view["skills"] if s["state"] == "deleted"],
    }
    files = pins.hash_tree(target / "skills")
    (target / "SKILL_REVIEW.json").write_text(
        json.dumps({**entry, "files": files}, indent=1), encoding="utf-8"
    )
    entries = _saved_index() + [{**entry, "file_count": len(files)}]
    _write_saved_index(entries)
    # Leave Scan and Library: the saved copy is now the reference.
    _delete_jobs([j for j in list(_jobs.values()) if j.source == repo_record.source])
    with _lock:
        _repos.pop(repo_id, None)
    _library.pop(repo_id, None)
    _save_repos()
    _save_library()
    shutil.rmtree(repo_record.root, ignore_errors=True)
    return entries[-1]


@app.get("/api/saved")
def saved() -> list[dict]:
    return sorted(_saved_index(), key=lambda e: -e["saved_at"])


@app.get("/api/saved/{entry_id}/download")
def saved_download(entry_id: str) -> FileResponse:
    entry = next((e for e in _saved_index() if e["id"] == entry_id), None)
    folder = _saved_dir() / entry_id
    if not entry or not folder.is_dir():
        raise HTTPException(404, "unknown saved entry")
    work = Path(tempfile.mkdtemp(prefix="skillui-saved-"))
    name = entry["name"].replace("/", "-")
    archive = shutil.make_archive(str(work / name), "zip", root_dir=folder)
    return FileResponse(archive, filename=f"{name}-saved.zip", media_type="application/zip")


@app.post("/api/saved/{entry_id}/verify")
def saved_verify(entry_id: str) -> dict:
    """Check the saved copy still matches the hashes recorded when it was saved."""
    folder = _saved_dir() / entry_id
    try:
        recorded = json.loads((folder / "SKILL_REVIEW.json").read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(404, "unknown saved entry") from exc
    current = pins.hash_tree(folder / "skills")
    changed = sorted(p for p in recorded.keys() & current.keys() if recorded[p] != current[p])
    added = sorted(current.keys() - recorded.keys())
    removed = sorted(recorded.keys() - current.keys())
    return {
        "clean": not (changed or added or removed),
        "changed": changed,
        "added": added,
        "removed": removed,
    }


@app.delete("/api/saved/{entry_id}")
def saved_delete(entry_id: str) -> dict:
    entries = _saved_index()
    if not any(e["id"] == entry_id for e in entries):
        raise HTTPException(404, "unknown saved entry")
    _write_saved_index([e for e in entries if e["id"] != entry_id])
    shutil.rmtree(_saved_dir() / entry_id, ignore_errors=True)
    return {"deleted": entry_id}


load_history()
load_repos()
load_library()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
