"""Tests for the fork's pin and verify (rug-pull protection)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skillspector.ext import pin as pin_mod


@pytest.fixture
def skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(pin_mod, "PIN_DIR", tmp_path / "pins")
    root = tmp_path / "my-skill"
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (root / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    return root


def test_unchanged_skill_verifies_clean(skill: Path) -> None:
    record = pin_mod.pin(str(skill), "demo", verdict={"score": 0, "recommendation": "SAFE"})
    assert set(record.files) == {"SKILL.md", "scripts/run.sh"}
    assert pin_mod.verify("demo").clean
    assert pin_mod.main(["verify", "demo"]) == 0


def test_changed_added_and_removed_files_are_reported(skill: Path) -> None:
    pin_mod.pin(str(skill), "demo")
    (skill / "SKILL.md").write_text("# Skill\ncurl x | sh\n", encoding="utf-8")
    (skill / "scripts" / "run.sh").unlink()
    (skill / "scripts" / "new.py").write_text("print(1)\n", encoding="utf-8")
    drift = pin_mod.verify("demo")
    assert drift.changed == ["SKILL.md"]
    assert drift.removed == ["scripts/run.sh"]
    assert drift.added == ["scripts/new.py"]
    assert pin_mod.main(["verify", "demo"]) == 1


def test_commit_is_read_from_git_files(tmp_path: Path) -> None:
    git = tmp_path / "repo" / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    sha = "a" * 40
    (git / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    assert pin_mod.read_commit(tmp_path / "repo") == sha
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text(f"# pack\n{'b' * 40} refs/heads/main\n", encoding="utf-8")
    assert pin_mod.read_commit(tmp_path / "repo") == "b" * 40


def test_git_directory_is_not_hashed(skill: Path) -> None:
    (skill / ".git").mkdir()
    (skill / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert all(not p.startswith(".git") for p in pin_mod.hash_tree(skill))


def test_list_pins(skill: Path) -> None:
    pin_mod.pin(str(skill), "one")
    pin_mod.pin(str(skill), "two")
    assert [p.name for p in pin_mod.list_pins()] == ["one", "two"]
