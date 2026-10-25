"""Run every probe skill in eval/probes through a static (--no-llm) scan and print a summary.

Each probe is a small skill containing one attack type (see docs/COVERAGE_AUDIT.md).
The probes are test fixtures only: they are scanned, never executed.

Usage:
    python eval/run_probes.py [--llm] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROBES_DIR = Path(__file__).parent / "probes"


def scan(probe: Path, out_dir: Path, use_llm: bool) -> dict:
    report = out_dir / f"{probe.name}.json"
    cmd = ["skillspector", "scan", str(probe), "--format", "json", "--output", str(report)]
    if not use_llm:
        cmd.append("--no-llm")
    subprocess.run(cmd, capture_output=True, check=False)
    return json.loads(report.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="enable the LLM stage")
    parser.add_argument("--out", type=Path, help="keep JSON reports in this directory")
    args = parser.parse_args()

    out_dir = args.out or Path(tempfile.mkdtemp(prefix="probe-reports-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    missed = 0
    for probe in sorted(p for p in PROBES_DIR.iterdir() if p.is_dir()):
        report = scan(probe, out_dir, args.llm)
        risk = report["risk_assessment"]
        ids = sorted({f"{i['id']}({i['severity'][0]})" for i in report["issues"]})
        if not ids:
            missed += 1
        print(
            f"{probe.name:18s} {risk['score']:>3} {risk['recommendation']:14s} {' '.join(ids) or '-- NONE --'}"
        )

    print(f"\n{missed} of {len(list(PROBES_DIR.iterdir()))} probes produced no finding")
    return 0


if __name__ == "__main__":
    sys.exit(main())
