"""Tests for the fork's named LLM profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from skillspector.ext import llm_profiles
from skillspector.ext.llm_profiles import (
    PROFILE_LABELS,
    PROFILES,
    profile_env,
    profile_ready,
    read_dotenv,
)


def test_sol_medium_sets_provider_model_and_effort() -> None:
    env = profile_env("sol-medium", base={"OPENAI_API_KEY": "k"})
    assert env["SKILLSPECTOR_PROVIDER"] == "openai"
    assert env["SKILLSPECTOR_MODEL"] == "gpt-6.1-sol"
    assert env["SKILLSPECTOR_REASONING_EFFORT"] == "medium"


def test_sonnet_clears_inherited_controls() -> None:
    base = {
        "ANTHROPIC_API_KEY": "k",
        "SKILLSPECTOR_TEMPERATURE": "0.2",
        "SKILLSPECTOR_REASONING_EFFORT": "max",
    }
    env = profile_env("sonnet", base=base)
    assert env["SKILLSPECTOR_MODEL"] == "claude-sonnet-5-5"
    assert "SKILLSPECTOR_TEMPERATURE" not in env
    assert "SKILLSPECTOR_REASONING_EFFORT" not in env


def test_sol_drops_temperature_it_rejects() -> None:
    env = profile_env("sol-medium", base={"OPENAI_API_KEY": "k", "SKILLSPECTOR_TEMPERATURE": "1"})
    assert "SKILLSPECTOR_TEMPERATURE" not in env


def test_missing_key_is_reported() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        profile_env("sol-medium", base={})


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown LLM profile"):
        profile_env("nope", base={})


def test_key_from_dotenv_and_process_env_wins(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("# comment\nOPENAI_API_KEY='from-file'\nOTHER=1\n", encoding="utf-8")
    assert read_dotenv(dotenv)["OPENAI_API_KEY"] == "from-file"
    assert profile_env("sol-medium", base={}, dotenv=dotenv)["OPENAI_API_KEY"] == "from-file"
    env = profile_env("sol-medium", base={"OPENAI_API_KEY": "from-env"}, dotenv=dotenv)
    assert env["OPENAI_API_KEY"] == "from-env"


def test_dotenv_inline_comments_are_stripped(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "LEVEL=WARNING      # options: DEBUG|INFO\nEMPTY=      # leave empty\nQ='a # b'\n",
        encoding="utf-8",
    )
    values = read_dotenv(dotenv)
    assert values == {"LEVEL": "WARNING", "EMPTY": "", "Q": "a # b"}


def test_every_profile_has_a_label() -> None:
    assert set(PROFILE_LABELS) == set(PROFILES)


def test_ollama_profile_ready_depends_on_pulled_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        llm_profiles.PROFILES,
        "local-test",
        {"SKILLSPECTOR_PROVIDER": "ollama", "SKILLSPECTOR_MODEL": "m:1"},
    )
    monkeypatch.setattr(llm_profiles, "ollama_models", lambda base_url=None: {"m:1"})
    assert profile_ready("local-test", {}) == (True, None)
    monkeypatch.setattr(llm_profiles, "ollama_models", lambda base_url=None: set())
    assert profile_ready("local-test", {}) == (False, "ollama pull m:1")


def test_no_weak_local_models_are_offered() -> None:
    assert not any(p["SKILLSPECTOR_PROVIDER"] == "ollama" for p in PROFILES.values())


def test_hosted_open_profile_needs_nvidia_key() -> None:
    assert profile_ready("nv-deepseek", {}) == (False, "NVIDIA_INFERENCE_KEY")
    assert profile_ready("nv-deepseek", {"NVIDIA_INFERENCE_KEY": "k"}) == (True, None)
