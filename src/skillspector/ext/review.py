"""Full fork review of one skill: fetch, scan, reveal, judge, explain, verdict.

    python -m skillspector.ext.review SOURCE --out result.json [--no-llm] [--no-strict]
    python -m skillspector.ext.review rewrite --input approved.json --out rewrite.json

SOURCE is anything upstream accepts (Git/HTTPS URL, zip, .md file, directory). Git
sources are cloned with the upstream hardened clone and without the operator's
credentials. Nothing from the skill is executed.

The LLM provider comes from the environment (SKILLSPECTOR_PROVIDER/MODEL), so callers
run this module with the environment of an LLM profile (see llm_profiles.py).

Verdict: the upstream score, plus the points of grounded bundle-judge findings, then the
strict floors. LLM stages can only raise the verdict, never lower it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from skillspector.ext.explain import explain
from skillspector.ext.judge import judge
from skillspector.ext.reveal import reveal_skill
from skillspector.ext.rewrite import rewrite
from skillspector.ext.strict_policy import strict_floor
from skillspector.input_handler import InputHandler
from skillspector.models import Finding
from skillspector.nodes.report import risk_severity_and_recommendation

SCAN_TIMEOUT_SECONDS = 900
_RANK = {"SAFE": 0, "CAUTION": 1, "DO_NOT_INSTALL": 2}


def _at_least(recommendation: str, minimum: str | None) -> str:
    """Never report a weaker recommendation than ``minimum`` (e.g. upstream fail-closed CAUTION)."""
    if minimum in _RANK and _RANK[minimum] > _RANK.get(recommendation, 0):
        return minimum  # type: ignore[return-value]
    return recommendation


def _upstream_scan(path: Path, use_llm: bool) -> tuple[dict, list[str]]:
    with tempfile.TemporaryDirectory(prefix="review-scan-") as tmp:
        out = Path(tmp) / "report.json"
        cmd = ["skillspector", "scan", str(path), "--format", "json", "--output", str(out)]
        if not use_llm:
            cmd.append("--no-llm")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT_SECONDS, check=False
        )
        if not out.is_file():
            raise RuntimeError(
                f"upstream scan failed (exit {result.returncode}): {result.stderr.strip()[-800:]}"
            )
        warnings = [
            line for line in result.stderr.splitlines() if "WARNING" in line or "ERROR" in line
        ]
        return json.loads(out.read_text(encoding="utf-8")), warnings[-20:]


def _as_finding(issue: dict) -> Finding:
    location = issue.get("location") or {}
    return Finding(
        rule_id=str(issue.get("id", "")),
        message=str(issue.get("finding", "")),
        severity=str(issue.get("severity", "LOW")),
        confidence=float(issue.get("confidence", 0.5)),
        file=str(location.get("file", "")),
        start_line=location.get("start_line"),
    )


def combined_verdict(
    report: dict,
    judge_output: dict | None,
    explanation: dict | None,
    strict: bool,
) -> dict:
    """Merge upstream and fork results into one verdict that LLM output can only raise."""
    upstream = report.get("risk_assessment", {})
    score = int(upstream.get("score") or 0)
    reasons = [f"upstream score {score} ({upstream.get('recommendation', '?')})"]
    issues = list(report.get("issues") or [])

    judge_findings = (judge_output or {}).get("findings") or []
    if judge_findings:
        points = sum(
            {"CRITICAL": 50, "HIGH": 25, "MEDIUM": 10, "LOW": 5}.get(f["severity"], 5)
            * f["confidence"]
            for f in judge_findings
        )
        score = min(100, int(score + points))
        reasons.append(f"bundle judge: {len(judge_findings)} grounded finding(s), +{int(points)}")
        issues += judge_findings

    floor = 0
    if strict:
        floor, strict_reasons = strict_floor(_as_finding(i) for i in issues)
        reasons += strict_reasons

    for stage, data in (("bundle judge", judge_output), ("explain", explanation)):
        if data and data.get("anomaly"):
            floor = max(floor, 21)
            reasons.append(f"{stage} anomaly: {data['anomaly']}")
    if judge_output and len(judge_output.get("ungrounded") or []) > max(2, len(judge_findings)):
        floor = max(floor, 21)
        reasons.append("bundle judge returned mostly unquotable findings")
    if explanation:
        uncovered = explanation.get("uncovered_lines", 0)
        meaningful = sum(c.get("meaningful_lines", 0) for c in explanation.get("coverage", []))
        if meaningful and uncovered / meaningful > 0.1:
            floor = max(floor, 21)
            reasons.append(f"explain left {uncovered}/{meaningful} lines undescribed")

    score = max(score, floor)
    severity, recommendation = risk_severity_and_recommendation(score)
    upstream_recommendation = upstream.get("recommendation")
    if _at_least(recommendation, upstream_recommendation) != recommendation:
        recommendation = upstream_recommendation
        reasons.append(
            f"upstream keeps {upstream_recommendation} "
            f"(analysis {report.get('analysis_completeness', {}).get('status', 'incomplete')})"
        )
    return {
        "score": score,
        "severity": severity,
        "recommendation": recommendation,
        "strict": strict,
        "reasons": reasons,
        "issues": issues,
    }


def add_dynamic(result: dict, sandbox: dict) -> dict:
    """Merge sandbox findings into a review result; the verdict can only go up."""
    findings = sandbox.get("findings") or []
    verdict = result["verdict"]
    issues = [i for i in verdict["issues"] if not str(i.get("id", "")).startswith("DY-")]
    issues += findings
    score = verdict["score"]
    reasons = [r for r in verdict["reasons"] if not r.startswith("sandbox:")]
    if findings:
        worst = max(
            findings, key=lambda f: ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(f["severity"])
        )
        floor = {"CRITICAL": 81, "HIGH": 51, "MEDIUM": 21, "LOW": 0}[worst["severity"]]
        score = max(score, floor)
        reasons.append(f"sandbox: {len(findings)} dynamic finding(s), worst {worst['severity']}")
    else:
        reasons.append("sandbox: no harmful behaviour observed (does not prove the skill is safe)")
    severity, recommendation = risk_severity_and_recommendation(score)
    recommendation = _at_least(recommendation, verdict.get("recommendation"))
    result["verdict"] = {
        **verdict,
        "score": score,
        "severity": severity,
        "recommendation": recommendation,
        "reasons": reasons,
        "issues": issues,
    }
    result["sandbox"] = sandbox
    return result


def review(source: str, *, use_llm: bool, strict: bool) -> dict:
    handler = InputHandler(allow_git_credentials=False)
    try:
        path, source_type = handler.resolve(source)
        report, warnings = _upstream_scan(path, use_llm)
        revealed = reveal_skill(path)
        raw_text = {}
        for item in revealed:
            try:
                raw_text[item.path] = (path / item.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
        judge_output = explanation = None
        if use_llm:
            judge_output = judge(revealed, raw_text).to_dict()
            explanation = explain(revealed).to_dict()
        verdict = combined_verdict(report, judge_output, explanation, strict)
        return {
            "source": source,
            "source_type": source_type,
            "report": report,
            "warnings": warnings,
            "reveal": [f.to_dict() for f in revealed if f.items],
            "files": [f.path for f in revealed],
            "judge": judge_output,
            "explanation": explanation,
            "verdict": verdict,
        }
    finally:
        handler.cleanup()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "rewrite":
        parser = argparse.ArgumentParser(prog="review rewrite")
        parser.add_argument("--input", type=Path, required=True, help="JSON: {purpose, items}")
        parser.add_argument("--out", type=Path, required=True)
        args = parser.parse_args(argv[1:])
        data = json.loads(args.input.read_text(encoding="utf-8"))
        result = rewrite(str(data.get("purpose", "")), list(data.get("items") or []))
        args.out.write_text(json.dumps(result.to_dict()), encoding="utf-8")
        return 0 if not result.error else 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-strict", action="store_true")
    args = parser.parse_args(argv)
    os.environ["SKILLSPECTOR_STRICT"] = "" if args.no_strict else "1"
    result = review(args.source, use_llm=not args.no_llm, strict=not args.no_strict)
    args.out.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
