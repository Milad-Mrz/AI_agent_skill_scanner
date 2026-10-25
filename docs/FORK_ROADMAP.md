# Fork Roadmap: AI_agent_skill_scanner (built on NVIDIA SkillSpector)

This fork adds the features from `../skill_checker_projector/DESIGN.md` on top of SkillSpector, and keeps it easy to pull upstream changes.

## 1. Git workflow

- `main` mirrors `upstream/main` (NVIDIA/SkillSpector) and gets **no fork commits**. Sync it often: `git fetch upstream && git merge --ff-only upstream/main`.
- `fork/main` is the integration branch for our features. It is rebased or merged onto `main` after each upstream sync.
- Each feature gets its own branch, `feat/<name>`, and a PR into `fork/main`.
- **To keep merge conflicts small:**
  - New code goes in new modules: new analyzer files, a new `src/skillspector/ext/` package, and new top-level `ui/`, `sandbox/` and `eval/` folders.
  - Edits to upstream files stay small: registry lines in `nodes/analyzers/__init__.py`, new state fields, new CLI subcommands.
- General improvements (a new analyzer, the strict LLM mode) can be offered back to NVIDIA as PRs. Their workflow requires DCO sign-off (`git commit -s`).
- Licensing: keep the Apache-2.0 `LICENSE`, `NOTICE` and SPDX headers, and mark our modified files.

## 2. Where our layers fit in SkillSpector's graph

```
resolve_input → build_context → [analyzers ‖] → meta_analyzer → report
```

| Our layer | SkillSpector today | Fork change |
|---|---|---|
| L0 fetch / quarantine | `input_handler.py` already does a shallow clone with an isolated git home and no stored credentials, plus byte, file and time limits that stop safely. | **Reuse it.** Add `ext/github_search.py` (search + trust score) outside the graph, for the UI. |
| L0 transitive fetch | `transitive.py`, `dependency_sources.py` (opt-in `--transitive`) | Check how far it goes first, then extend it if needed. |
| L1 static | 22 analyzers, including `unicode_confusables`, `whitespace_padding`, `static_yara`, AST and taint tracking | New analyzer nodes (registry entries only): see §3 |
| L1b classifier | none | New optional node `static_prompt_guard` (extra dependency `[guard]`) |
| L3 LLM judge | `semantic_*` analyzers + `meta_analyzer`. **The meta-analyzer can drop static findings as false positives.** | **Strict mode** (§3.1) and a whole-bundle judge with quoted spans checked against the files |
| L2 / UI | Command line only; LangGraph Studio for development | New `ui/` app that calls `graph.stream()` for live progress |
| L5 reveal / paraphrase / rewrite | none | New CLI subcommands `reveal`, `explain`, `rewrite` in `ext/` |
| L4 sandbox | none | New `sandbox/` project. Its dynamic findings are converted to `Finding` objects and merged into the SARIF report. |
| Evaluation | `docs/EVAL_DATASETS.md` exists | New `eval/` folder: Skill-Inject and SkillTrustBench runners, plus the IDR metric |

## 3. Feature list

### 3.1 Strict verdict mode (done: `feat/strict-llm`)
**Correction to the first draft:** the current upstream `meta_analyzer` never drops a static finding. Findings the LLM disputes are tagged `llm-unconfirmed` and keep their confidence. The real problem found by the coverage audit is **scoring**: clear attacks with HIGH findings scored 20–50, giving SAFE or CAUTION.
- `SKILLSPECTOR_STRICT=1` turns on `src/skillspector/ext/strict_policy.py`. It applies floors that can only raise the score:
  - any CRITICAL finding: at least 81 (DO_NOT_INSTALL)
  - HIGH findings from two independent sources (a static rule plus an LLM analyzer, or two LLM analyzers): at least 51 (DO_NOT_INSTALL)
  - any HIGH finding: at least 21 (CAUTION)
- Disputed (`llm-unconfirmed`) findings still count.
- The hook in `nodes/report.py` is 6 lines.
- A degraded LLM stage already turns SAFE into CAUTION upstream, and the UI shows a banner for it.

### 3.2 New static analyzers (done: `feat/static-gaps`, `static_fork_gaps.py` FG1–FG12)
Each item: first check what upstream already covers, then add only what's missing.
- `static_patterns_guardfall`: normalize shell text before matching (remove quotes inside words, join split strings, expand `$'\xNN'`, `${a}${b}`, `printf` building), then run the existing exfiltration and execution patterns again on the result.
- `static_encoded_payloads`: decode base16/32/64/85, binary, Morse, ROT-n, URL encoding, punycode and nested layers, then rescan the decoded text. Content that can't be decoded but isn't readable either is flagged too.
- `static_oov_score`: an unknown-word score per line using a dictionary plus a technical vocabulary. Lines that score high are flagged.
- `static_persistence`: writes to `CLAUDE.md`, memory folders, `settings.json`, hooks, shell rc files or cron. Compare with `static_patterns_memory_poisoning` first.
- `static_trigger_breadth`: frontmatter `description` written to match everything, or name collisions with other skills. Check the existing trigger-abuse rules first.
- `static_hidden_markup`: HTML comments, `<details>`, alt text and link titles carrying instructions.

### 3.3 Whole-bundle judge (SkillSecurer style) (done: `feat/bundle-judge`, `ext/judge.py`, `ext/review.py`)
- New semantic node `semantic_bundle_judge`: one call sees the whole skill. It checks stated purpose against actual capabilities, input sources against sinks (indirect injection), and dormant or conditional logic.
- Output is structured findings with **quoted spans**. A span that can't be found in the file cache is dropped (see `llm_provenance.py` and `security_reconstruction.py`, which may already help).
- Refusal detection with automatic fallback to another model.

### 3.4 Reveal, explain and rewrite (done: `feat/reveal-explain`; these run as stages of `ext/review.py` and in the UI rather than as separate CLI subcommands)
- `skillspector reveal <skill>`: produces a copy with invisible characters tagged, homoglyphs marked, blobs decoded and hidden markup pulled out. Pure code, no model.
- `skillspector explain <skill>`: the model, with no tools, turns every instruction into `{source, plain_english, category, risk}`. A code check then confirms every line is covered: uncovered lines are flagged in red, and disagreements with the static findings are highlighted.
- `skillspector rewrite <skill> --approve items.json`: builds a clean `SKILL.md` from the approved items, placing safety steps on the path the agent actually follows. Scripts are left untouched.

### 3.5 UI (done: `feat/ui`, `src/skillspector/ext/webui/`)
- Input: GitHub search (with trust score), a URL or a zip upload.
- Live progress from `graph.stream`.
- Findings table: file, line, severity, rule, escaped code with context, decoded values.
- The reveal and explain views, and an "Open in ESET" button where the user records ESET's verdict.
- Export to JSON, SARIF or HTML.

### 3.6 Pin and rescan (done: `feat/pin-rescan`, `ext/pin.py`, Pins tab)
`skillspector pin` stores the commit and content hashes. `skillspector verify` rescans and compares against the pinned version, so a skill that changes after review is caught. Reuse the baseline and fingerprint code in `suppression.py`.

### 3.7 Sandbox (done, Docker backend: `feat/sandbox`, `ext/sandbox/`, docs/SANDBOX.md; KVM backend still planned)
- KVM/libvirt VM image with honey files, canary tokens and auditd/eBPF monitoring.
- mitmproxy add-on on the host that swaps the fake key for the real one, enforces limits, scans outgoing traffic for canaries and blocks everything else.
- Several trigger prompts per skill, plus mock tickets, PRs and webpages carrying attack text.
- Output: dynamic `Finding` objects merged into the report.

### 3.8 Evaluation (`eval/`)
- Runners for Skill-Inject (165 cases) and SkillTrustBench `safe_pool`.
- Metrics: IDR (did it find the injected text itself), GDR (did it flag the skill at all), false-positive rate and cost.
- CI job: static-only regression on fixtures. LLM runs are triggered by hand.

## 4. Phases

| # | Branch | Scope | Depends on |
|---|---|---|---|
| 0 | `feat/coverage-audit` | Map the threat table T1–T17 to existing rules and analyzers, and list the real gaps. Set up the dev environment and get the tests passing. | none |
| 1 | `feat/strict-llm` | §3.1 | 0 |
| 2 | `feat/static-gaps` | §3.2 (only the gaps phase 0 confirms) | 0 |
| 3 | `feat/ui` | §3.5 (MVP: scan + findings table) | 0 |
| 4 | `feat/bundle-judge` | §3.3 | 1 |
| 5 | `feat/reveal-explain` | §3.4 | 2 |
| 6 | `feat/eval` | §3.8 (ideally earlier, to measure phases 1–5) | 0 |
| 7 | `feat/pin-rescan` | §3.6 | 0 |
| 8 | `sandbox/` | §3.7 | 3 |

## 5. Decisions (2026-10-25)
1. UI: **FastAPI**.
2. LLM profiles (`src/skillspector/ext/llm_profiles.py`, used by `eval/run_probes.py --profile` and the future UI):
   - `sonnet`: provider `anthropic`, model `claude-sonnet-5-5` (default)
   - `sol-medium`: provider `openai`, model `gpt-6.1-sol`, `SKILLSPECTOR_REASONING_EFFORT=medium`
   Keys come from the environment or the gitignored `.env` (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`).
3. Upstream PRs to NVIDIA: not at this point.
