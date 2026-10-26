# Web UI (fork)

A local FastAPI interface for scanning skills and searching GitHub.

```bash
uv pip install -e ".[ui]"            # or: pip install -e ".[ui]"
python -m skillspector.ext.webui     # http://127.0.0.1:8765  (localhost only)
```

## Batch flow (Scan tab)
1. **Repositories:** paste one or more GitHub links or local paths. Each one is fetched once (upstream hardened clone, no credentials) and split into its skills. A tile shows the GitHub logo, the name and the skill count. Click a tile to select it.
2. **Operations:**
   - **SkillSpector-MM:** static rules plus reveal, free.
   - **Model reviews:** one tile per model (upstream LLM stage plus the bundle judge).
   - **Explain:** plain-English instructions plus a coverage check. It's slow, so it's a separate tile.
   - **Sandbox:** a real agent with fake secrets.
   - **Pin:** one per repository.
3. **Run:** every (skill × operation) pair becomes a task.
   - **Progress:** each task shows its stages live (fetch, upstream-scan, reveal, judge, explain, verdict).
   - **Queue:** LLM tasks run one at a time (`SKILLSPECTOR_UI_LLM_WORKERS`) to avoid provider throttling. Static tasks run three at a time.
   - **Adding more:** add repositories and click Run again while tasks run; new tasks join the waiting list. Queued or running tasks can be cancelled.
   - **Time limit:** 2400 s per task (`SKILLSPECTOR_UI_TASK_TIMEOUT`). A timeout now reports the stage it stopped in.
4. **Runs** are grouped by repository link.
   - **Expanding a repository** lists its skills, and each skill lists its runs.
   - **Per repository:** *Run selected methods* (change method), *Redo failed*, *Stop* and *Remove scans*.
   - **Per task:** *Open*, *Redo* and *Cancel*.
   - **Tokens and cost:** every task shows its token count and estimated cost. The **API usage** panel totals tokens and estimated cost per model.
   - **Prices:** estimates use list prices that you can edit in `~/.local/share/skill-scanner/pricing.json`. Provider dashboards show the actual bill.
5. **Themes:** the header button switches between light (white background, blue buttons, black text, the default) and dark (black background, green buttons, white text). The choice is remembered in the browser.

## Features
- **Scan:** a Git/HTTPS URL, a local path, or an uploaded `.zip` or `.md` file.
- **Model:** grouped by type. Profiles are defined in `src/skillspector/ext/llm_profiles.py`.
  - Hosted frontier models: Claude Sonnet 5.5, and GPT-6.1 Sol (medium effort).
  - Open-weight models hosted on NVIDIA Build: DeepSeek V4 Flash, GLM-5.3 and gpt-oss-120b. These need `NVIDIA_INFERENCE_KEY`.
  - Local open-weight models through Ollama: Qwen2.5-Coder 7B and 14B. These need no key, and the skill never leaves the machine.

  A profile is selectable only when its key is set in the environment or `.env`, or, for local models, when the model has been pulled into Ollama.
- **Strict verdict** (on by default): applies `SKILLSPECTOR_STRICT=1` floors. See `docs/FORK_ROADMAP.md` §3.1.
- **Pipeline:** every scan runs `python -m skillspector.ext.review` with these stages:
  1. the upstream scan;
  2. **reveal**, pure code;
  3. the **bundle judge**, one LLM review of the whole skill, keeping only findings whose quote is found in the files;
  4. **explain**, a plain-English list of every instruction plus a coverage check;
  5. a **combined verdict** that the LLM stages can only raise.
- **Results:** score, verdict and the reasons for it, the models used and token counts, a summary of each stage, and warnings and anomalies.
- **Findings view:** upstream, fork-rule (FG) and bundle-judge (BJ) findings together, with severity filters. Expanding a finding shows its explanation, code, evidence and remediation.
- **Hidden content view:** every invisible character, look-alike letter, decoded blob, piece of hidden markup and unreadable token, with the fully revealed text of each file.
- **Instructions view:** the plain-English instructions with their risk, the share of lines described per file, and the lines the model left out, shown in red.
- **Clean rewrite:** tick the instructions to keep (high and critical ones start unticked), then click *Build clean rewrite*. A new `SKILL.md` is written from the kept plain-English instructions only, re-checked, and offered as a download.
- **Check URL on ESET:** copies the skill URL and opens the ESET AI Skills Checker, which has no API.
- **Export JSON:** downloads the full report.
- **Search GitHub:** uses repository metadata only and computes a trust score (age, star growth, forks, issues, owner type, license, and contributors via "Details"). "Scan" sends a repository straight to the scanner.
- **Pin this version** (on a finished scan) and the **Pins** tab: a pin stores the commit and a SHA-256 hash of every file (`~/.local/share/skill-scanner/pins/`). **Verify** fetches the source again and lists changed, added and removed files, which catches a skill that changes after review. The same works from the command line: `python -m skillspector.ext.pin pin|verify|list`.
- **Skill Library** (formerly History): decide which skills to keep.
  - **Colours:** each skill shows its worst verdict across every method (green, yellow or red, or empty when not scanned).
  - **Group deletes:** *Delete red*, *Delete red + yellow*, *Delete selected*, *Keep selected* and *Restore all*.
  - **Cleaned:** a repository is marked **CLEANED** when every remaining skill is green.
  - **Export kept skills:** a zip of the remaining skills plus `SKILL_REVIEW.json` (the verdicts, and the list of removed skills).
  - **Separation from scans:** deleting in the library doesn't remove scan results. Scans are managed on the Scan page.
- **Saved Skills:** the permanent store, for fully cleaned repositories only.
  - **When Save unlocks:** every remaining skill needs ✓ static, ✓ LLM, ✓ explain and ✓ sandbox, with no yellow or red result. The library shows that checklist on every skill, and the Save button lists what's missing.
  - **One passing LLM is enough:** LLM model reviews are alternatives, so one API model with a SAFE verdict satisfies the LLM check, and a second model that disagrees doesn't block the skill. Static, Explain and Sandbox must each pass. This combined verdict drives the library colours, group deletes, CLEANED and Save, while every model's own result stays visible.
  - **How to get there:** fix and redo scans one by one, or delete the red and yellow skills.
  - **What Save does:** copies the skills with `SKILL_REVIEW.json` (verdicts, methods, commit, file hashes) to `~/.local/share/skill-scanner/saved/`, and removes the repository from Scan and the Library.
  - **On the Saved page:** download a zip, verify the files against their hashes, or delete.
- **Combined results:** grouped by link, across all runs.
  - Each link lists its skills with the latest verdict of every method and the merged severity counts.
  - **View all results** (or a click on one skill) opens a combined view, with:
    - a card per method (score, verdict, date, link to the full report)
    - one merged findings list, where each finding shows which methods found it
    - the hidden content, instructions and sandbox run together
  - Links can be filtered, or deleted with all their results.
- **History (storage):** every scan is saved to `~/.local/share/skill-scanner/scans/` (change it with `SKILLSPECTOR_UI_DATA`), so history survives restarts and crashes. Scans that were still running when the server stopped are shown as *interrupted*. Scans can be deleted.

## Safety
- The server binds to `127.0.0.1` only.
- Each scan runs the `skillspector` CLI in a subprocess.
- The page inserts all skill-derived text with `textContent` and never renders skill markdown.
- API keys are never sent to the browser. `/api/profiles` reports only whether a key is present.
