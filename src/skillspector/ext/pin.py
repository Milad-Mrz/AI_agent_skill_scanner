"""Pin a reviewed skill version and detect later changes (rug-pull protection).

    python -m skillspector.ext.pin pin SOURCE [--name NAME] [--verdict-file review.json]
    python -m skillspector.ext.pin verify NAME
    python -m skillspector.ext.pin list

A pin records the source, the Git commit (read from the clone's .git files, no git
command is run inside the untrusted repository) and a SHA-256 hash of every file.
``verify`` fetches the source again and reports added, removed and changed files.
Exit code 1 means the skill changed since it was pinned and must be reviewed again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from skillspector.input_handler import InputHandler

PIN_DIR = (
    Path(
        os.environ.get(
            "SKILLSPECTOR_UI_DATA",
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            / "skill-scanner",
        )
    )
    / "pins"
)
_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Pin:
    name: str
    source: str
    commit: str | None
    files: dict[str, str]
    pinned_at: float = field(default_factory=time.time)
    verdict: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Drift:
    name: str
    changed: list[str]
    added: list[str]
    removed: list[str]
    old_commit: str | None
    new_commit: str | None

    @property
    def clean(self) -> bool:
        return not (self.changed or self.added or self.removed)

    def to_dict(self) -> dict:
        return {**asdict(self), "clean": self.clean}


def read_commit(root: Path) -> str | None:
    """Return the checked-out commit by reading .git files directly."""
    git = None
    for candidate in [root, *root.parents][:4]:
        if (candidate / ".git" / "HEAD").is_file():
            git = candidate / ".git"
            break
    if git is None:
        return None
    head = (git / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
    if re.fullmatch(r"[0-9a-f]{40,64}", head):
        return head
    if not head.startswith("ref: "):
        return None
    ref = head[5:].strip()
    if ".." in ref:
        return None
    ref_file = git / ref
    if ref_file.is_file():
        return ref_file.read_text(encoding="utf-8", errors="ignore").strip() or None
    packed = git / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def hash_tree(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts or path.is_symlink() or not path.is_file():
            continue
        digests[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _snapshot(source: str) -> tuple[str | None, dict[str, str]]:
    handler = InputHandler(allow_git_credentials=False)
    try:
        path, _kind = handler.resolve(source)
        return read_commit(path), hash_tree(path)
    finally:
        handler.cleanup()


def _pin_path(name: str) -> Path:
    return PIN_DIR / f"{_NAME.sub('-', name).strip('-') or 'skill'}.json"


def default_name(source: str) -> str:
    return _NAME.sub("-", source.rstrip("/").removesuffix(".git").split("/")[-1]) or "skill"


def pin(source: str, name: str | None = None, verdict: dict | None = None) -> Pin:
    commit, files = _snapshot(source)
    record = Pin(name or default_name(source), source, commit, files, verdict=verdict)
    path = _pin_path(record.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), indent=1), encoding="utf-8")
    return record


def load(name: str) -> Pin:
    data = json.loads(_pin_path(name).read_text(encoding="utf-8"))
    return Pin(**data)


def list_pins() -> list[Pin]:
    if not PIN_DIR.is_dir():
        return []
    pins = []
    for path in sorted(PIN_DIR.glob("*.json")):
        try:
            pins.append(Pin(**json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return pins


def compare(old: Pin, commit: str | None, files: dict[str, str]) -> Drift:
    return Drift(
        name=old.name,
        changed=sorted(p for p in old.files.keys() & files.keys() if old.files[p] != files[p]),
        added=sorted(files.keys() - old.files.keys()),
        removed=sorted(old.files.keys() - files.keys()),
        old_commit=old.commit,
        new_commit=commit,
    )


def verify(name: str) -> Drift:
    old = load(name)
    commit, files = _snapshot(old.source)
    return compare(old, commit, files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_pin = sub.add_parser("pin")
    p_pin.add_argument("source")
    p_pin.add_argument("--name")
    p_pin.add_argument("--verdict-file", type=Path, help="review JSON whose verdict to store")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("name")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    if args.command == "pin":
        verdict = None
        if args.verdict_file:
            verdict = json.loads(args.verdict_file.read_text(encoding="utf-8")).get("verdict")
            verdict = {k: verdict[k] for k in ("score", "recommendation")} if verdict else None
        record = pin(args.source, args.name, verdict)
        print(f"pinned {record.name}: {len(record.files)} files, commit {record.commit or 'n/a'}")
        return 0
    if args.command == "verify":
        drift = verify(args.name)
        print(json.dumps(drift.to_dict(), indent=1))
        return 0 if drift.clean else 1
    for record in list_pins():
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.pinned_at))
        print(f"{record.name:30s} {when}  {record.commit or '-':12.12s}  {record.source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
