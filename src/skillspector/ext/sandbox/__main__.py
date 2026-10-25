"""CLI: python -m skillspector.ext.sandbox SKILL_DIR --profile NAME [--out dyn.json]."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from skillspector.ext.llm_profiles import PROFILES
from skillspector.ext.sandbox import run_sandbox
from skillspector.input_handler import InputHandler

REPO_ROOT = Path(__file__).resolve().parents[4]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a skill with an agent in the sandbox")
    parser.add_argument("source", help="skill directory, zip, .md file or Git/HTTPS URL")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="local-qwen-coder-7b")
    parser.add_argument("--prompt", action="append", help="task prompt (repeatable)")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dotenv", type=Path, default=REPO_ROOT / ".env", help="API key file")
    args = parser.parse_args()
    handler = InputHandler(allow_git_credentials=False)
    try:
        skill_dir, _kind = handler.resolve(args.source)
        result = run_sandbox(
            skill_dir, args.profile, args.prompt, args.max_steps, dotenv=args.dotenv
        )
    finally:
        handler.cleanup()
    data = result.to_dict()
    for finding in data["findings"]:
        print(
            f"{finding['id']:5s} {finding['severity']:8s} {finding['finding']}: "
            f"{json.dumps(finding['evidence']['observed'])[:160]}"
        )
    print(
        f"error: {data['error']}"
        if data["error"]
        else f"{len(data['findings'])} finding(s); {data['events'].get('llm_calls')} LLM calls, "
        f"{data['events'].get('seconds')} s"
    )
    if args.out:
        args.out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return 0 if not data["error"] else 2


if __name__ == "__main__":
    sys.exit(main())
