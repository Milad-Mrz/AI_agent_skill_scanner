# AI Agent Skill Scanner (fork of NVIDIA SkillSpector)

This fork adds layers on top of [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) for deciding whether an AI agent skill is safe to install, and for installing a clean version of it. Upstream code is unchanged apart from small hooks. Fork code lives in `src/skillspector/ext/` and `src/skillspector/nodes/analyzers/static_fork_gaps.py`.

## Quick start

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev,ui]"
# API keys go in the gitignored .env: OPENAI_API_KEY, ANTHROPIC_API_KEY, NVIDIA_INFERENCE_KEY
python -m skillspector.ext.webui          # http://127.0.0.1:8765  (see README.md for the step-by-step flow)
```

From the command line:

```bash
python -m skillspector.ext.review <url|dir|zip> --out review.json [--no-llm]
python -m skillspector.ext.sandbox <url|dir> --profile sol-medium --out dyn.json
python -m skillspector.ext.pin pin|verify|list ...
python eval/run_probes.py [--profile sol-medium] [--strict]
python eval/benchmark.py red|benign --root <SkillSecurer clone> [--profile ...]
```

## Pipeline

| Stage | What it does | Code |
|---|---|---|
| Fetch | Upstream hardened clone without credentials (zip and .md inputs too) | upstream `input_handler.py` |
| Upstream scan | 71 patterns, YARA, AST, taint, plus the optional upstream LLM stage | upstream |
| Fork rules FG1–FG12 | Untrusted package sources, trigger abuse, persistence, encoded payloads, GuardFall obfuscation, resource abuse, indirect injection, shell interpolation, untrusted PR code, sandbox evasion and time bombs, result falsification, credential stores | `static_fork_gaps.py` |
| Reveal | Invisible, bidi and tag characters, homoglyphs, decoded base64/hex/binary/Morse, hidden markup, unreadable tokens (no model) | `ext/reveal.py` |
| Bundle judge | One tool-free LLM review of the whole skill; findings must quote the files | `ext/judge.py` |
| Explain + coverage | Plain-English list of every instruction; lines left undescribed are flagged | `ext/explain.py` |
| Verdict | Upstream score + judge points + strict floors + anomaly checks; never lower than upstream | `ext/review.py`, `ext/strict_policy.py` |
| Clean rewrite | New SKILL.md from the approved plain-English instructions only, re-checked | `ext/rewrite.py` |
| Sandbox | A real agent runs the skill in Docker, with no internet, canary secrets, strace and a host gateway | `ext/sandbox/`, docs/SANDBOX.md |
| Pin / verify | Commit plus per-file SHA-256; detects changes after review | `ext/pin.py` |
| Web UI | Batch scans with live stages and a queue; API usage; Skill Library (keep/delete, CLEANED, export); Saved Skills | `ext/webui/`, docs/WEBUI.md |

## Models (`ext/llm_profiles.py`)
- Claude Sonnet 5.5
- GPT-6.1 Sol (medium effort)
- Open-weight models hosted on NVIDIA Build: DeepSeek V4 Flash, GLM-5.3 and gpt-oss-120b
- Local Ollama models are supported for custom profiles, but none are bundled: in testing, the small local models were too weak

## Results
See `docs/COVERAGE_AUDIT.md` (28 probes) and `docs/BENCHMARK.md` (SkillSecurer datasets).

## Docs
- `docs/FORK_ROADMAP.md`: plan and status
- `docs/WEBUI.md`: the UI
- `docs/SANDBOX.md`: the dynamic analysis sandbox and its limits
- `docs/COVERAGE_AUDIT.md`, `docs/BENCHMARK.md`: measurements
