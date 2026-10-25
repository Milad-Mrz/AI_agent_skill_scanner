# Web UI (fork)

A local FastAPI interface for scanning skills and searching GitHub.

```bash
uv pip install -e ".[ui]"            # or: pip install -e ".[ui]"
python -m skillspector.ext.webui     # http://127.0.0.1:8765  (localhost only)
```

## Features
- **Scan:** a Git/HTTPS URL, a local path, or an uploaded `.zip` or `.md` file.
- **Model:** Static only, Claude Sonnet 5.5, or GPT-6.1 Sol (medium effort). Profiles are defined in `src/skillspector/ext/llm_profiles.py`. A profile is selectable only when its API key is set in the environment or the repository's `.env`.
- **Results:** score, verdict, which models were used and how many tokens, warnings (for example a degraded LLM stage), and the list of findings with severity filters. Expanding a finding shows its explanation, code, evidence and remediation.
- **Check URL on ESET:** copies the skill URL and opens the ESET AI Skills Checker, which has no API.
- **Export JSON:** downloads the full report.
- **Search GitHub:** uses repository metadata only and computes a trust score (age, star growth, forks, issues, owner type, license, and contributors via "Details"). "Scan" sends a repository straight to the scanner.
- **History:** scans from the current server session.

## Safety
- The server binds to `127.0.0.1` only.
- Each scan runs the `skillspector` CLI in a subprocess.
- The page inserts all skill-derived text with `textContent` and never renders skill markdown.
- API keys are never sent to the browser. `/api/profiles` reports only whether a key is present.
