"""Named LLM profiles for the fork's tools (probe runner, UI).

A profile is a set of SKILLSPECTOR_* environment variables that selects a provider,
a model and optional controls. Upstream reads these variables; nothing here changes
how the scanner itself resolves providers.

API keys are never stored in a profile. They come from the environment or from the
repository's gitignored ``.env`` file.
"""

from __future__ import annotations

import os
import re
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
    # Open-weight models hosted on NVIDIA Build (free key at build.nvidia.com).
    "nv-deepseek": {
        "SKILLSPECTOR_PROVIDER": "nv_build",
        "SKILLSPECTOR_MODEL": "deepseek-ai/deepseek-v4-flash-0731",
    },
    "nv-glm": {
        "SKILLSPECTOR_PROVIDER": "nv_build",
        "SKILLSPECTOR_MODEL": "z-ai/glm-5.3",
    },
    "nv-gpt-oss": {
        "SKILLSPECTOR_PROVIDER": "nv_build",
        "SKILLSPECTOR_MODEL": "openai/gpt-oss-120b",
    },
    # Local open-weight models through Ollama; nothing leaves the machine.
    "local-qwen-coder-7b": {
        "SKILLSPECTOR_PROVIDER": "ollama",
        "SKILLSPECTOR_MODEL": "qwen2.5-coder:7b",
    },
    "local-qwen-coder-14b": {
        "SKILLSPECTOR_PROVIDER": "ollama",
        "SKILLSPECTOR_MODEL": "qwen2.5-coder:14b",
    },
}

PROFILE_LABELS: dict[str, str] = {
    "sonnet": "Claude Sonnet 5.5",
    "sol-medium": "GPT-6.1 Sol (medium effort)",
    "nv-deepseek": "DeepSeek V4 Flash (open, NVIDIA Build)",
    "nv-glm": "GLM-5.3 (open, NVIDIA Build)",
    "nv-gpt-oss": "gpt-oss-120b (open, NVIDIA Build)",
    "local-qwen-coder-7b": "Qwen2.5-Coder 7B (local, Ollama)",
    "local-qwen-coder-14b": "Qwen2.5-Coder 14B (local, Ollama, slow on 6 GB)",
}

# The API key each profile needs, so callers can fail early with a clear message.
# Local Ollama profiles need no key.
REQUIRED_KEY: dict[str, str | None] = {
    "sonnet": "ANTHROPIC_API_KEY",
    "sol-medium": "OPENAI_API_KEY",
    "nv-deepseek": "NVIDIA_INFERENCE_KEY",
    "nv-glm": "NVIDIA_INFERENCE_KEY",
    "nv-gpt-oss": "NVIDIA_INFERENCE_KEY",
    "local-qwen-coder-7b": None,
    "local-qwen-coder-14b": None,
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
        value = value.strip()
        if value[:1] in {"'", '"'} and value[1:].find(value[0]) != -1:
            value = value[1 : value.index(value[0], 1)]
        else:
            # Unquoted values may carry an inline comment ("VALUE   # note").
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if value.startswith("#"):
                value = ""
        values[key.strip()] = value
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
    key_name = REQUIRED_KEY.get(name)
    if key_name and not env.get(key_name):
        raise ValueError(f"Profile {name!r} needs {key_name} in the environment or .env")
    return env


def ollama_models(base_url: str | None = None, timeout: float = 2.0) -> set[str]:
    """Names of models the local Ollama server has pulled; empty when it is not running."""
    import httpx

    root = (base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1").rstrip(
        "/"
    )
    root = root.removesuffix("/v1")
    try:
        response = httpx.get(f"{root}/api/tags", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError:
        return set()
    return {item.get("name", "") for item in response.json().get("models", [])}


def profile_ready(name: str, env: Mapping[str, str]) -> tuple[bool, str | None]:
    """Whether a profile can run now, and what is missing if not."""
    if PROFILES[name]["SKILLSPECTOR_PROVIDER"] == "ollama":
        model = PROFILES[name]["SKILLSPECTOR_MODEL"]
        if model in ollama_models(env.get("OLLAMA_BASE_URL")):
            return True, None
        return False, f"ollama pull {model}"
    key_name = REQUIRED_KEY.get(name)
    if key_name and not env.get(key_name):
        return False, key_name
    return True, None
