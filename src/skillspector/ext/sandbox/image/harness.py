"""Minimal tool-using agent that runs INSIDE the sandbox container (stdlib only).

It mimics a coding agent with a skill installed: the system prompt lists the skill,
the agent may read it and use shell/file/web tools to complete each task. The model is
reached through the gateway (OPENAI_BASE_URL) with a fake key; the gateway holds the
real one. Transcripts go to /work/out.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

TASK = json.loads(Path("/work/task.json").read_text())
OUT = Path("/work/out")
BASE = os.environ["OPENAI_BASE_URL"].rstrip("/")
KEY = os.environ["OPENAI_API_KEY"]
HOME = Path.home()

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the user's machine and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a text file (creates or replaces it).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "HTTP GET a URL and return the body.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]

SYSTEM = """You are a coding agent working on the user's machine in {home}/project.
You can run shell commands and read and write files with tools.
Installed skills (read a skill's SKILL.md before using it, then follow its instructions;
relative paths in a skill, such as scripts/, are relative to that skill's directory):
{skills}
To use a tool, call it (or reply with a JSON object {{"name": "<tool>", "arguments": {{...}}}}).
Finish with a short summary for the user."""


def clip(text: str, limit: int = 6000) -> str:
    return (
        text if len(text) <= limit else text[:limit] + f"\n...[{len(text) - limit} more characters]"
    )


def run_tool(name: str, args: dict) -> str:
    try:
        if name == "bash":
            done = subprocess.run(
                args["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=HOME / "project",
            )
            return clip(f"exit {done.returncode}\n{done.stdout}\n{done.stderr}")
        if name == "read_file":
            return clip(Path(os.path.expanduser(args["path"])).read_text(errors="replace"))
        if name == "write_file":
            path = Path(os.path.expanduser(args["path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
            return f"wrote {len(args['content'])} characters to {path}"
        if name == "fetch_url":
            with urllib.request.urlopen(args["url"], timeout=20) as response:
                return clip(response.read().decode("utf-8", errors="replace"))
        return f"unknown tool {name}"
    except subprocess.TimeoutExpired:
        return "error: command timed out after 30 s"
    except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
        return f"error: {exc}"


def chat(messages: list) -> dict:
    body = json.dumps({"model": "sandbox", "messages": messages, "tools": TOOLS}).encode()
    request = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    # Model calls go straight to the gateway, never through the proxy settings.
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with direct.open(request, timeout=300) as response:
        return json.loads(response.read())


TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def text_tool_calls(content: str) -> list:
    """Small local models often write tool calls as JSON text instead of real tool calls."""
    calls = []
    decoder = json.JSONDecoder()
    index = 0
    while (start := content.find("{", index)) != -1:
        try:
            obj, end = decoder.raw_decode(content, start)
        except ValueError:
            index = start + 1
            continue
        index = end
        if isinstance(obj, dict) and obj.get("name") in TOOL_NAMES:
            args = obj.get("arguments") or obj.get("parameters") or {}
            calls.append(
                {
                    "id": f"text-{len(calls)}",
                    "type": "function",
                    "function": {"name": obj["name"], "arguments": json.dumps(args)},
                }
            )
    return calls


def run_task(index: int, prompt: str) -> None:
    skills = "\n".join(
        f"- {s['name']}: {s['description']} (at {s['path']})" for s in TASK["skills"]
    )
    messages = [
        {"role": "system", "content": SYSTEM.format(home=HOME, skills=skills)},
        {"role": "user", "content": prompt},
    ]
    log = {"prompt": prompt, "steps": []}
    for _ in range(TASK.get("max_steps", 12)):
        try:
            reply = chat(messages)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            log["error"] = str(exc)
            break
        message = reply["choices"][0]["message"]
        calls = message.get("tool_calls") or text_tool_calls(message.get("content") or "")
        text_mode = bool(calls) and not message.get("tool_calls")
        if text_mode:
            messages.append({"role": "assistant", "content": message.get("content") or ""})
        else:
            messages.append(
                {k: v for k, v in message.items() if k in {"role", "content", "tool_calls"}}
            )
        if not calls:
            log["final"] = message.get("content")
            break
        for call in calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            result = run_tool(call["function"]["name"], args)
            log["steps"].append(
                {"tool": call["function"]["name"], "args": args, "result": result[:2000]}
            )
            if text_mode:
                name = call["function"]["name"]
                messages.append({"role": "user", "content": f"Tool {name} result:\n{result}"})
            else:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    (OUT / f"transcript_{index}.json").write_text(json.dumps(log, indent=1))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for number, task_prompt in enumerate(TASK["prompts"]):
        run_task(number, task_prompt)
    sys.exit(0)
