"""Run every probe skill in eval/probes through SkillSpector and print a summary.

Each probe is a small skill containing one attack type (see docs/COVERAGE_AUDIT.md).
The probes are test fixtures only: they are scanned, never executed.

Usage:
    python eval/run_probes.py                       # static (--no-llm)
    python eval/run_probes.py --profile sonnet      # Claude Sonnet 5.5
    python eval/run_probes.py --profile sol-medium  # GPT-6.1 Sol, medium reasoning effort
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from skillspector.ext.llm_profiles import PROFILES, profile_env

PROBES_DIR = Path(__file__).parent / "probes"
REPO_ROOT = Path(__file__).resolve().parent.parent


def scan(probe: Path, out_dir: Path, env: dict[str, str], use_llm: bool) -> dict:
    report = out_dir / f"{probe.name}.json"
    cmd = ["skillspector", "scan", str(probe), "--format", "json", "--output", str(report)]
    if not use_llm:
        cmd.append("--no-llm")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if not report.is_file():
        raise RuntimeError(
            f"{probe.name}: scan failed (exit {result.returncode}): {result.stderr[-500:]}"
        )
    return json.loads(report.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--profile", choices=sorted(PROFILES), help="enable the LLM stage with this profile"
    )
    parser.add_argument("--out", type=Path, help="keep JSON reports in this directory")
    parser.add_argument("--strict", action="store_true", help="apply the strict verdict policy")
    args = parser.parse_args()

    if args.profile:
        try:
            env = profile_env(args.profile, dotenv=REPO_ROOT / ".env")
        except ValueError as exc:
            parser.error(str(exc))
    else:
        env = dict(os.environ)

    if args.strict:
        env["SKILLSPECTOR_STRICT"] = "1"

    out_dir = args.out or Path(tempfile.mkdtemp(prefix="probe-reports-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    probes = sorted(p for p in PROBES_DIR.iterdir() if p.is_dir())
    missed = 0
    for probe in probes:
        report = scan(probe, out_dir, env, use_llm=bool(args.profile))
        risk = report["risk_assessment"]
        ids = sorted({f"{i['id']}({i['severity'][0]})" for i in report["issues"]})
        if not ids:
            missed += 1
        print(
            f"{probe.name:18s} {risk['score']:>3} {risk['recommendation']:14s} {' '.join(ids) or '-- NONE --'}"
        )

    print(
        f"\n{missed} of {len(probes)} probes produced no finding (profile: {args.profile or 'static'})"
    )
    print(f"Reports: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
