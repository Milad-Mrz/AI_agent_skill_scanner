"""GitHub skill search with a metadata-only trust score.

Only repository metadata is requested (stars, forks, issues, dates, owner type).
No README or file content is downloaded here, so nothing from a candidate skill
reaches a model or the UI as rendered markup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import httpx

API = "https://api.github.com"
_TIMEOUT = 15.0


def _token() -> str | None:
    """GITHUB_TOKEN from the environment, else the logged-in gh CLI token, else None."""
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def _client() -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token := _token():
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API, headers=headers, timeout=_TIMEOUT)


@dataclass
class RepoTrust:
    full_name: str
    html_url: str
    description: str
    owner_type: str
    stars: int
    forks: int
    open_issues: int
    created_at: str
    pushed_at: str
    archived: bool
    license: str | None
    age_days: int
    stars_per_day: float
    contributors: int | None = None
    score: int = 0
    level: str = "unknown"
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _days_since(iso: str, now: datetime) -> int:
    return max((now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days, 0)


def score_repo(
    repo: dict, contributors: int | None = None, now: datetime | None = None
) -> RepoTrust:
    """Compute a 0-100 trust score from repository metadata alone.

    The score is a triage hint, not a verdict: farmed stars, young repos and
    anonymous owners lower it; age, real issue activity, organisations and
    several contributors raise it.
    """
    now = now or datetime.now(UTC)
    age = _days_since(repo["created_at"], now)
    stars = int(repo.get("stargazers_count", 0))
    forks = int(repo.get("forks_count", 0))
    issues = int(repo.get("open_issues_count", 0))
    per_day = stars / max(age, 1)
    owner_type = repo.get("owner", {}).get("type", "User")
    trust = RepoTrust(
        full_name=repo["full_name"],
        html_url=repo["html_url"],
        description=(repo.get("description") or "")[:300],
        owner_type=owner_type,
        stars=stars,
        forks=forks,
        open_issues=issues,
        created_at=repo["created_at"],
        pushed_at=repo.get("pushed_at") or repo["created_at"],
        archived=bool(repo.get("archived")),
        license=(repo.get("license") or {}).get("spdx_id"),
        age_days=age,
        stars_per_day=round(per_day, 1),
        contributors=contributors,
    )

    score = 50
    signals: list[str] = []
    if age >= 365:
        score += 15
        signals.append("+ older than one year")
    elif age < 60:
        score -= 15
        signals.append("- created less than 60 days ago")
    if age < 90 and per_day > 50:
        score -= 30
        signals.append(f"- star spike: {per_day:.0f} stars/day on a {age}-day-old repo")
    if stars >= 1000 and issues < 5:
        score -= 10
        signals.append("- many stars but almost no open issues")
    elif issues >= 20:
        score += 5
        signals.append("+ active issue tracker")
    if stars >= 100 and forks < stars / 100:
        score -= 10
        signals.append("- very few forks for the star count")
    if owner_type == "Organization":
        score += 10
        signals.append("+ owned by an organisation")
    if contributors is not None:
        if contributors >= 5:
            score += 10
            signals.append(f"+ {contributors} contributors")
        elif contributors <= 1:
            score -= 10
            signals.append("- single contributor")
    if trust.archived:
        score -= 10
        signals.append("- archived")
    if _days_since(trust.pushed_at, now) > 365:
        score -= 5
        signals.append("- no push in over a year")
    if not trust.license:
        score -= 5
        signals.append("- no license")

    trust.score = max(0, min(100, score))
    trust.level = "high" if trust.score >= 70 else "medium" if trust.score >= 45 else "low"
    trust.signals = signals
    return trust


def search_skills(query: str, limit: int = 20) -> list[dict]:
    """Search GitHub repositories for skills matching ``query``, sorted by stars."""
    q = f"{query} skill" if "skill" not in query.lower() else query
    with _client() as client:
        response = client.get(
            "/search/repositories",
            params={"q": q, "sort": "stars", "order": "desc", "per_page": min(limit, 50)},
        )
        response.raise_for_status()
        items = response.json().get("items", [])
    return [score_repo(item).to_dict() for item in items]


def repo_details(full_name: str) -> dict:
    """Fetch one repository plus its contributor count and return the scored result."""
    with _client() as client:
        repo = client.get(f"/repos/{full_name}")
        repo.raise_for_status()
        contributors = client.get(
            f"/repos/{full_name}/contributors", params={"per_page": 100, "anon": "false"}
        )
        count = len(contributors.json()) if contributors.status_code == 200 else None
    return score_repo(repo.json(), contributors=count).to_dict()
