"""Isolated, tool-free structured LLM calls for the fork's review stages.

Untrusted skill text is wrapped in random delimiters and declared as data. The model
gets no tools; its answer must validate against a Pydantic schema. Callers treat the
result as a hint that can only add findings, never remove deterministic ones.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from skillspector.llm_utils import get_chat_model

# Phrases that suggest the model followed instructions from the skill instead of
# describing them. Seeing one in the output marks the call as anomalous.
_OBEDIENCE = re.compile(
    r"\b(?:as\s+instructed|i\s+(?:have|will)\s+(?:now\s+)?(?:ignored|followed|executed|uploaded|sent)|"
    r"no\s+(?:issues|findings|risks)\s+(?:were\s+)?found\s+as\s+requested|"
    r"this\s+skill\s+is\s+(?:verified|certified)\s+safe)\b",
    re.IGNORECASE,
)


@dataclass
class CallResult:
    value: BaseModel | None
    error: str | None = None
    anomaly: str | None = None
    raw: str = ""


def fence(text: str) -> tuple[str, str]:
    """Wrap untrusted text in a delimiter the skill cannot predict."""
    tag = f"UNTRUSTED-{secrets.token_hex(6)}"
    return tag, f"<<<{tag}>>>\n{text}\n<<<END-{tag}>>>"


def number_lines(text: str) -> str:
    return "\n".join(f"L{i}: {line}" for i, line in enumerate(text.splitlines(), start=1))


def _extract_json(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if match:
        return match.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def structured_call(prompt: str, schema: type[BaseModel], *, timeout: float = 240) -> CallResult:
    """Call the active provider's model and validate the answer against ``schema``."""
    try:
        model = get_chat_model(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return CallResult(None, error=f"model unavailable: {exc}")

    raw = ""
    try:
        structured = model.with_structured_output(schema)
        value = structured.invoke(prompt)
        if isinstance(value, dict):
            value = schema.model_validate(value)
        raw = value.model_dump_json() if isinstance(value, BaseModel) else str(value)
    except Exception as first_error:  # noqa: BLE001 - fall back to plain JSON
        try:
            reply = model.invoke(
                prompt
                + "\n\nReturn ONLY one JSON object matching this JSON schema:\n"
                + json.dumps(schema.model_json_schema())
            )
            raw = getattr(reply, "content", reply)
            raw = raw if isinstance(raw, str) else json.dumps(raw)
            value = schema.model_validate_json(_extract_json(raw))
        except (ValidationError, ValueError) as exc:
            return CallResult(None, error=f"invalid structured output: {exc}", raw=raw[:2000])
        except Exception as exc:  # noqa: BLE001
            return CallResult(None, error=f"model call failed: {first_error} / {exc}")

    anomaly = None
    if match := _OBEDIENCE.search(raw):
        anomaly = f"output contains {match.group(0)!r}: the model may have followed the skill"
    return CallResult(value, anomaly=anomaly, raw=raw[:20000])
