"""Repository fetching and skill discovery for batch runs.

A repository is fetched once with the upstream hardened clone (no credentials) into a
cache directory; every directory that holds a SKILL.md becomes a separate skill, so
a 25-skill repository turns into 25 small tasks instead of one huge scan.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from skillspector.input_handler import InputHandler

MAX_SKILLS = 200
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
_NAME = re.compile(r"^name:\s*['\"]?([^'\"\n]+)", re.M)


@dataclass
class Skill:
    path: str  # relative to the repository root ("." for a root skill)
    name: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FetchedRepo:
    source: str
    name: str
    root: Path
    skills: list[Skill] = field(default_factory=list)
    truncated: bool = False


def repo_name(source: str) -> str:
    """Short display name: owner/repo[/sub/path] for GitHub URLs, else the last path part."""
    match = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)(?:/tree/[^/]+/?(.*))?", source)
    if match:
        owner, repo, sub = match.group(1), match.group(2).removesuffix(".git"), match.group(3)
        return f"{owner}/{repo}" + (f"/{sub.strip('/')}" if sub and sub.strip("/") else "")
    return Path(source.rstrip("/")).name or source


def cache_key(source: str) -> str:
    return hashlib.sha256(source.strip().encode()).hexdigest()[:16]


def discover_skills(root: Path) -> tuple[list[Skill], bool]:
    """Every directory containing SKILL.md, shallowest first; root only if nothing else."""
    found: list[Skill] = []
    stack = [root]
    while stack and len(found) <= MAX_SKILLS:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        skill_md = directory / "SKILL.md"
        if skill_md.is_file() and not skill_md.is_symlink():
            text = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
            match = _NAME.search(text)
            relative = str(directory.relative_to(root)) or "."
            found.append(
                Skill(relative, (match.group(1).strip() if match else directory.name)[:80])
            )
        stack.extend(
            e
            for e in reversed(entries)
            if e.is_dir() and not e.is_symlink() and e.name not in _SKIP_DIRS
        )
    found.sort(key=lambda s: (s.path != ".", s.path))
    # A root SKILL.md next to sub-skills is usually an index; keep it but list sub-skills too.
    return found[:MAX_SKILLS], len(found) > MAX_SKILLS


def fetch(source: str, cache_dir: Path) -> FetchedRepo:
    """Fetch ``source`` once into ``cache_dir/<key>`` and discover its skills."""
    target = cache_dir / cache_key(source)
    if target.exists():
        shutil.rmtree(target)
    handler = InputHandler(allow_git_credentials=False)
    try:
        path, _kind = handler.resolve(source)
        shutil.copytree(path, target, symlinks=True)
    finally:
        handler.cleanup()
    skills, truncated = discover_skills(target)
    return FetchedRepo(source, repo_name(source), target, skills, truncated)
