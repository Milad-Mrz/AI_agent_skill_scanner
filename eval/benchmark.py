"""Benchmark the fork review pipeline on public skill-security datasets.

Datasets (clone them outside this repository; they are data and are never executed):

    git clone --depth 1 https://github.com/MacD1998/SkillSecurer.git
    git clone --depth 1 https://github.com/aisa-group/skill-inject.git

Modes:
    red     SkillSecurer red-generated set: 20 base skills x 9 threat types (180 cases).
            Ground truth is the line diff between the base SKILL.md and the injected one.
    benign  SkillSecurer's 130 likely-benign skills (SkillTrustBench safe pool), for the
            false-positive rate.

Metrics (as in the SkillSecurer paper):
    GDR  generic detection rate: the case gets at least one finding of MEDIUM or higher
    IDR  injection detection rate: a finding (MEDIUM or higher) overlaps the injected lines
    plus the verdict distribution and, for benign skills, the share flagged.

Usage:
    python eval/benchmark.py red    --root PATH/SkillSecurer --out red-static.json
    python eval/benchmark.py red    --root PATH/SkillSecurer --profile sol-medium --per-type 2
    python eval/benchmark.py benign --root PATH/SkillSecurer --limit 30
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from skillspector.ext.llm_profiles import PROFILES, profile_env

RED_DIR = "dataset/dataset_red_on_skillTrustBenchVerified_20"
BASE_DIR = "dataset/skillTrustBenchVerified_20"
BENIGN_DIR = "dataset/SkillTrustBench/secureSkills130"
REPO_ROOT = Path(__file__).resolve().parent.parent
SEVERE = {"MEDIUM", "HIGH", "CRITICAL"}
LINE_SLACK = 2


def injected_lines(base: str, injected: str) -> set[int]:
    """Line numbers (1-based, in the injected file) that the injection added or changed."""
    lines: set[int] = set()
    matcher = difflib.SequenceMatcher(
        None, base.splitlines(), injected.splitlines(), autojunk=False
    )
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "insert"}:
            lines.update(range(j1 + 1, j2 + 1))
    return lines


def red_cases(root: Path, per_type: int | None) -> list[dict]:
    cases = []
    counts: Counter[str] = Counter()
    for path in sorted((root / RED_DIR / "injected").glob("case_*/*.md")):
        case_id, threat, _model = path.stem.split("__")
        if per_type is not None and counts[threat] >= per_type:
            continue
        base_dir = root / BASE_DIR / case_id
        if not (base_dir / "SKILL.md").is_file():
            continue
        counts[threat] += 1
        cases.append({"id": path.stem, "threat": threat, "injected": path, "base_dir": base_dir})
    return cases


def benign_cases(root: Path, limit: int | None) -> list[dict]:
    dirs = sorted(p for p in (root / BENIGN_DIR).iterdir() if (p / "SKILL.md").is_file())
    return [{"id": p.name, "threat": "benign", "dir": p} for p in dirs[:limit]]


def run_review(skill_dir: Path, env: dict[str, str], use_llm: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="bench-") as tmp:
        out = Path(tmp) / "review.json"
        cmd = [sys.executable, "-m", "skillspector.ext.review", str(skill_dir), "--out", str(out)]
        if not use_llm:
            cmd.append("--no-llm")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
        if not out.is_file():
            return {"error": result.stderr.strip()[-500:] or f"exit {result.returncode}"}
        return json.loads(out.read_text(encoding="utf-8"))


def _overlaps(issue: dict, lines: set[int]) -> bool:
    location = issue.get("location") or {}
    if location.get("file", "SKILL.md") != "SKILL.md":
        return False
    start = location.get("start_line") or 0
    end = location.get("end_line") or start
    return any(start - LINE_SLACK <= n <= end + LINE_SLACK for n in lines)


def evaluate_red(case: dict, env: dict[str, str], use_llm: bool) -> dict:
    base = (case["base_dir"] / "SKILL.md").read_text(encoding="utf-8")
    injected = case["injected"].read_text(encoding="utf-8")
    truth = injected_lines(base, injected)
    with tempfile.TemporaryDirectory(prefix="bench-skill-") as tmp:
        skill = Path(tmp) / case["base_dir"].name
        shutil.copytree(case["base_dir"], skill, symlinks=False)
        (skill / "SKILL.md").write_text(injected, encoding="utf-8")
        review = run_review(skill, env, use_llm)
    if "error" in review:
        return {**_meta(case), "error": review["error"]}
    issues = [i for i in review["verdict"]["issues"] if i.get("severity") in SEVERE]
    hits = sorted({i["id"] for i in issues if _overlaps(i, truth)})
    return {
        **_meta(case),
        "injected_lines": sorted(truth),
        "gdr": bool(issues),
        "idr": bool(hits),
        "idr_static": any(not str(r).startswith(("SDI-", "SQP-", "SSD-", "BJ-")) for r in hits),
        "idr_judge": any(str(r).startswith("BJ-") for r in hits),
        "idr_fork_rules": any(str(r).startswith("FG") for r in hits),
        "idr_upstream": any(not str(r).startswith(("FG", "BJ-")) for r in hits),
        "hit_rules": hits,
        "score": review["verdict"]["score"],
        "recommendation": review["verdict"]["recommendation"],
        "upstream_recommendation": review["report"]["risk_assessment"]["recommendation"],
    }


def evaluate_benign(case: dict, env: dict[str, str], use_llm: bool) -> dict:
    review = run_review(case["dir"], env, use_llm)
    if "error" in review:
        return {**_meta(case), "error": review["error"]}
    issues = review["verdict"]["issues"]
    return {
        **_meta(case),
        "high_findings": sorted(
            {i["id"] for i in issues if i.get("severity") in {"HIGH", "CRITICAL"}}
        ),
        "score": review["verdict"]["score"],
        "recommendation": review["verdict"]["recommendation"],
        "upstream_recommendation": review["report"]["risk_assessment"]["recommendation"],
    }


def _meta(case: dict) -> dict:
    return {"id": case["id"], "threat": case["threat"]}


def summarize(mode: str, rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    summary: dict = {"cases": len(rows), "errors": len(rows) - len(ok)}
    summary["verdicts"] = dict(Counter(r["recommendation"] for r in ok))
    summary["upstream_verdicts"] = dict(Counter(r["upstream_recommendation"] for r in ok))
    if mode == "red":
        for key in ("gdr", "idr", "idr_upstream", "idr_static", "idr_fork_rules", "idr_judge"):
            summary[key] = round(100 * sum(r[key] for r in ok) / max(len(ok), 1), 1)
        per_type: dict[str, list[dict]] = defaultdict(list)
        for row in ok:
            per_type[row["threat"]].append(row)
        summary["by_threat"] = {
            threat: {
                "n": len(items),
                "idr": round(100 * sum(r["idr"] for r in items) / len(items), 1),
                "blocked": sum(r["recommendation"] == "DO_NOT_INSTALL" for r in items),
            }
            for threat, items in sorted(per_type.items())
        }
    else:
        flagged = [r for r in ok if r["recommendation"] != "SAFE"]
        summary["flagged_percent"] = round(100 * len(flagged) / max(len(ok), 1), 1)
        summary["do_not_install_percent"] = round(
            100 * sum(r["recommendation"] == "DO_NOT_INSTALL" for r in ok) / max(len(ok), 1), 1
        )
        summary["rules_on_benign"] = dict(
            Counter(rule for r in ok for rule in r["high_findings"]).most_common(15)
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("mode", choices=["red", "benign"])
    parser.add_argument("--root", type=Path, required=True, help="SkillSecurer repository clone")
    parser.add_argument("--profile", choices=sorted(PROFILES), help="LLM profile (default: static)")
    parser.add_argument("--per-type", type=int, help="red: at most N cases per threat type")
    parser.add_argument("--limit", type=int, help="benign: at most N skills")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--out", type=Path, help="write rows and summary as JSON")
    parser.add_argument("--dotenv", type=Path, default=REPO_ROOT / ".env", help="API key file")
    args = parser.parse_args()

    use_llm = bool(args.profile)
    env = profile_env(args.profile, dotenv=args.dotenv) if use_llm else dict(os.environ)
    if args.mode == "red":
        cases, worker = red_cases(args.root, args.per_type), evaluate_red
    else:
        cases, worker = benign_cases(args.root, args.limit), evaluate_benign

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda c: worker(c, env, use_llm), cases))
    summary = summarize(args.mode, rows)
    summary["profile"] = args.profile or "static"
    summary["seconds"] = round(time.time() - started)
    print(json.dumps(summary, indent=2))
    if args.out:
        args.out.write_text(
            json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
