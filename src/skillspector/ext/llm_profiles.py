"""Named LLM profiles for the fork's tools (probe runner, UI).

A profile is a set of SKILLSPECTOR_* environment variables that selects a provider,
a model and optional controls. Upstream reads these variables; nothing here changes
how the scanner itself resolves providers.

API keys are never stored in a profile. They come from the environment or from the
repository's gitignored ``.env`` file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

PROFILES: dict[str, dict[str, str]] = {
    "sonnet": {
        "SKILLSPECTOR_PROVIDER": "anthropic",
        "SKILLSPECTOR_MODEL": "claude-sonnet-5-5",
    },
    "sol-medium": {
        "SKILLSPECTOR_PROVIDER": "openai",
        "SKILLSPECTOR_MODEL": "gpt-6.1-sol",
        "SKILLSPECTOR_REASONING_EFFORT": "medium",
    },
}

# The API key each profile needs, so callers can fail early with a clear message.
REQUIRED_KEY: dict[str, str] = {
    "sonnet": "ANTHROPIC_API_KEY",
    "sol-medium": "OPENAI_API_KEY",
}

# Controls a profile must not inherit from the caller's environment, because the
# target model rejects or misreads them (gpt-6.1-sol rejects every explicit temperature).
_CLEARED_CONTROLS = ("SKILLSPECTOR_TEMPERATURE", "SKILLSPECTOR_REASONING_EFFORT")


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines from a .env file; missing file gives an empty dict."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def profile_env(
    name: str,
    base: Mapping[str, str] | None = None,
    dotenv: Path | None = None,
) -> dict[str, str]:
    """Return a full environment for running the scanner with profile ``name``.

    Precedence: profile settings > process environment > .env file.
    """
    if name not in PROFILES:
        raise ValueError(f"Unknown LLM profile {name!r}; choose from {', '.join(PROFILES)}")
    env = read_dotenv(dotenv) if dotenv else {}
    env.update(base if base is not None else os.environ)
    for key in _CLEARED_CONTROLS:
        env.pop(key, None)
    env.update(PROFILES[name])
    key_name = REQUIRED_KEY[name]
    if not env.get(key_name):
        raise ValueError(f"Profile {name!r} needs {key_name} in the environment or .env")
    return env
