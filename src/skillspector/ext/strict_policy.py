"""Strict verdict policy (fork, opt-in with SKILLSPECTOR_STRICT=1).

Upstream scoring adds confidence-weighted points per finding, so one clear HIGH
finding (25 points) can still end as SAFE, and several HIGH findings often stop
at CAUTION. The coverage audit (docs/COVERAGE_AUDIT.md) showed attacks such as
"ignore previous instructions" or falsified reports scoring 20-50.

The strict policy only ever raises the score, through floors:

- any CRITICAL finding                                   -> at least 81 (DO_NOT_INSTALL)
- HIGH findings confirmed by two independent sources     -> at least 51 (DO_NOT_INSTALL)
  (a static rule plus an LLM analyzer, or two different LLM analyzers)
- any HIGH finding                                       -> at least 21 (CAUTION)

Findings the LLM disputed (tag ``llm-unconfirmed``) still count: an injected
judge must not be able to talk a finding out of the verdict.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from skillspector.models import Finding

STRICT_ENV = "SKILLSPECTOR_STRICT"

# Rule-id prefixes emitted by the LLM-backed semantic analyzers.
_SEMANTIC_PREFIXES = ("SDI-", "SQP-", "SSD-")
_MIN_CONFIDENCE = 0.5


def strict_enabled() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _source(finding: Finding) -> str:
    """Group a finding by the analyzer family that produced it."""
    rule_id = finding.rule_id or ""
    for prefix in _SEMANTIC_PREFIXES:
        if rule_id.startswith(prefix):
            return prefix.rstrip("-")
    return "static"


def strict_floor(findings: Iterable[Finding]) -> tuple[int, list[str]]:
    """Return the minimum score the strict policy requires and the reasons."""
    critical = 0
    high_sources: set[str] = set()
    for finding in findings:
        if max(0.0, min(1.0, finding.confidence)) < _MIN_CONFIDENCE:
            continue
        severity = (finding.severity or "").upper()
        if severity == "CRITICAL":
            critical += 1
            high_sources.add(_source(finding))
        elif severity == "HIGH":
            high_sources.add(_source(finding))

    if critical:
        return 81, [f"strict: {critical} CRITICAL finding(s)"]
    if len(high_sources) >= 2:
        return 51, [f"strict: HIGH findings from {len(high_sources)} independent sources"]
    if high_sources:
        return 21, ["strict: HIGH finding present"]
    return 0, []
