"""Run only the fork gap analyzer (FG1-FG12) over a directory and count hits per rule.

Used to check recall on eval/probes and false positives on known-good skill collections:
    python eval/fork_rules_check.py eval/probes --per-dir
    python eval/fork_rules_check.py /path/to/anthropics/skills
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from skillspector.nodes.analyzers import static_fork_gaps

SUFFIXES = {".md", ".py", ".sh", ".js", ".ts", ".txt", ".json", ".yaml", ".yml", ".toml"}


def files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in SUFFIXES and ".git" not in path.parts:
            if path.stat().st_size < 1_000_000:
                yield path


def hits(path: Path, root: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return static_fork_gaps.analyze(text, str(path.relative_to(root)), path.suffix.lstrip("."))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--per-dir", action="store_true", help="summarize per top-level directory")
    args = parser.parse_args()
    if args.per_dir:
        for sub in sorted(p for p in args.root.iterdir() if p.is_dir()):
            rules = sorted({f.rule_id for path in files(sub) for f in hits(path, sub)})
            print(f"{sub.name:18s} {' '.join(rules) or '-'}")
        return
    counts: Counter[str] = Counter()
    total = 0
    for path in files(args.root):
        total += 1
        for finding in hits(path, args.root):
            counts[finding.rule_id] += 1
            print(
                f"{finding.rule_id:5s} {path.relative_to(args.root)}:{finding.location.start_line}: "
                f"{finding.matched_text[:100]!r}"
            )
    print(f"\n{total} files, {sum(counts.values())} findings: {dict(counts)}")


if __name__ == "__main__":
    main()
