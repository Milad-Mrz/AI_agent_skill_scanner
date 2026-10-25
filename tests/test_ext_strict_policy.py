"""Tests for the fork's strict verdict policy."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skillspector.ext.strict_policy import strict_enabled, strict_floor
from skillspector.models import Finding

PROBES = Path(__file__).resolve().parents[1] / "eval" / "probes"


def _finding(rule_id: str, severity: str, confidence: float = 0.8, tags=None) -> Finding:
    return Finding(
        rule_id=rule_id,
        message="m",
        severity=severity,
        confidence=confidence,
        file="SKILL.md",
        start_line=1,
        tags=list(tags or []),
    )


def test_no_findings_no_floor() -> None:
    assert strict_floor([]) == (0, [])


def test_single_high_is_caution() -> None:
    assert strict_floor([_finding("P1", "HIGH")])[0] == 21


def test_high_from_static_and_llm_blocks_install() -> None:
    assert strict_floor([_finding("P1", "HIGH"), _finding("SDI-1", "HIGH")])[0] == 51


def test_two_llm_analyzers_count_as_independent() -> None:
    assert strict_floor([_finding("SDI-1", "HIGH"), _finding("SSD-1", "HIGH")])[0] == 51


def test_two_static_rules_are_one_source() -> None:
    assert strict_floor([_finding("P1", "HIGH"), _finding("PE3", "HIGH")])[0] == 21


def test_critical_blocks_install() -> None:
    assert strict_floor([_finding("SDI-1", "CRITICAL")])[0] == 81


def test_llm_disputed_finding_still_counts() -> None:
    disputed = _finding("P1", "HIGH", tags=["llm-unconfirmed"])
    assert strict_floor([disputed, _finding("SSD-1", "HIGH")])[0] == 51


def test_low_confidence_is_ignored() -> None:
    assert strict_floor([_finding("P1", "CRITICAL", confidence=0.3)])[0] == 0


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("", False), ("0", False)])
def test_strict_env(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("SKILLSPECTOR_STRICT", value)
    assert strict_enabled() is expected


def _scan(env_extra: dict[str, str], tmp_path: Path) -> dict:
    import os

    out = tmp_path / "r.json"
    env = {**os.environ, **env_extra}
    subprocess.run(
        [
            "skillspector",
            "scan",
            str(PROBES / "T01_direct"),
            "--no-llm",
            "--format",
            "json",
            "--output",
            str(out),
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    return json.loads(out.read_text())["risk_assessment"]


def test_strict_raises_static_verdict_end_to_end(tmp_path: Path) -> None:
    default = _scan({"SKILLSPECTOR_STRICT": ""}, tmp_path)
    strict = _scan({"SKILLSPECTOR_STRICT": "1"}, tmp_path)
    assert default["recommendation"] == "SAFE"
    assert strict["recommendation"] == "CAUTION"
    assert strict["score"] >= 21
