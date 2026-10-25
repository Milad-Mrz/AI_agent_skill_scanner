# Web UI (fork)

A local FastAPI interface for scanning skills and searching GitHub.

```bash
uv pip install -e ".[ui]"            # or: pip install -e ".[ui]"
python -m skillspector.ext.webui     # http://127.0.0.1:8765  (localhost only)
```

## Features
- **Scan:** a Git/HTTPS URL, a local path, or an uploaded `.zip` or `.md` file.
- **Model:** grouped by type. Profiles are defined in `src/skillspector/ext/llm_profiles.py`.
  - Hosted frontier models: Claude Sonnet 5.5, and GPT-6.1 Sol (medium effort).
  - Open-weight models hosted on NVIDIA Build: DeepSeek V4 Flash, GLM-5.3 and gpt-oss-120b. These need `NVIDIA_INFERENCE_KEY`.
  - Local open-weight models through Ollama: Qwen2.5-Coder 7B and 14B. These need no key, and the skill never leaves the machine.

  A profile is selectable only when its key is set in the environment or `.env`, or, for local models, when the model has been pulled into Ollama.
- **Strict verdict** (on by default): applies `SKILLSPECTOR_STRICT=1` floors. See `docs/FORK_ROADMAP.md` §3.1.
- **Results:** score, verdict, which models were used and how many tokens, warnings (for example a degraded LLM stage), and the list of findings with severity filters. Expanding a finding shows its explanation, code, evidence and remediation.
- **Check URL on ESET:** copies the skill URL and opens the ESET AI Skills Checker, which has no API.
- **Export JSON:** downloads the full report.
- **Search GitHub:** uses repository metadata only and computes a trust score (age, star growth, forks, issues, owner type, license, and contributors via "Details"). "Scan" sends a repository straight to the scanner.
- **History:** every scan is saved to `~/.local/share/skill-scanner/scans/` (change it with `SKILLSPECTOR_UI_DATA`), so history survives restarts and crashes. Scans that were still running when the server stopped are shown as *interrupted*. Scans can be deleted.

## Safety
- The server binds to `127.0.0.1` only.
- Each scan runs the `skillspector` CLI in a subprocess.
- The page inserts all skill-derived text with `textContent` and never renders skill markdown.
- API keys are never sent to the browser. `/api/profiles` reports only whether a key is present.
