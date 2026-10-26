"""Estimated API cost from token counts.

Prices are USD per million tokens (input, output). The defaults come from public
price listings (October 2026) and are estimates; override them in
``~/.local/share/skill-scanner/pricing.json`` as {"model": [input, output]}.
NVIDIA Build's free developer tier is listed at 0.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-6.1-sol": (2.00, 10.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "deepseek-ai/deepseek-v4.1-flash": (0.0, 0.0),
    "z-ai/glm-5.3": (0.0, 0.0),
    "moonshotai/kimi-k2.6": (0.0, 0.0),
}

PRICING_FILE = (
    Path(
        os.environ.get(
            "SKILLSPECTOR_UI_DATA",
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            / "skill-scanner",
        )
    )
    / "pricing.json"
)


def prices() -> dict[str, tuple[float, float]]:
    table = dict(DEFAULT_PRICES)
    try:
        for model, pair in json.loads(PRICING_FILE.read_text(encoding="utf-8")).items():
            table[model] = (float(pair[0]), float(pair[1]))
    except (OSError, ValueError, TypeError, IndexError, KeyError):
        pass
    return table


def estimate(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated USD cost, or None when the model has no known price."""
    if not model or model not in (table := prices()):
        return None
    price_in, price_out = table[model]
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 4)
