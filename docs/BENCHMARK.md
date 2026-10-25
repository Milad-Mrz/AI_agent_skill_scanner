# Benchmark: SkillSecurer datasets (2026-10-25)

**Datasets:** from the SkillSecurer repository (Mecca et al., arXiv 2609.14079), cloned as data and never executed.
- **Red:** 20 base skills × 9 threat types = 180 injected skills. The ground truth is the line diff between each base and injected `SKILL.md`.
- **Benign:** 130 likely-safe skills (the SkillTrustBench safe pool).

**Metrics:**
- **GDR:** the share of cases with any finding of MEDIUM or higher.
- **IDR:** a MEDIUM-or-higher finding overlaps the injected lines (±2).

```bash
python eval/benchmark.py red    --root <SkillSecurer> --out red-static.json
python eval/benchmark.py red    --root <SkillSecurer> --profile sol-medium --per-type 2
python eval/benchmark.py benign --root <SkillSecurer>
```

Raw rows are in `eval/results/`.

## Injected skills (red)

| Run | Cases | GDR | IDR | IDR, upstream rules (+ its LLM stage) | IDR, fork FG rules | IDR, bundle judge | DO NOT INSTALL |
|---|---|---|---|---|---|---|---|
| Static (`--no-llm`) | 180 | 70.6% | **39.4%** | 34.4% | 6.1% | n/a | 4 (2%) |
| GPT-6.1 Sol, 2 per threat type | 18 | 94.4% | **88.9%** | 88.9% | 5.6% | 61.1% | 14 (78%) |

The sources overlap, so the per-source columns don't add up to the total.

**IDR by threat type** (static on 20 cases each, Sol on 2 cases each):

| Threat | Static IDR | Sol IDR |
|---|---|---|
| privilege escalation | 95% | 2/2 |
| GuardFall | 75% | 2/2 |
| data exfiltration | 45% | 2/2 |
| credential exposure | 40% | 1/2 |
| prompt hijacking | 35% | 2/2 |
| indirect injection | 30% | 2/2 |
| arbitrary script execution | 15% | 2/2 |
| resource exhaustion | 15% | 2/2 |
| supply chain | 5% | 1/2 |

**What this shows:**
- **Static rules alone miss most context-dependent injections.** That matches the paper, where upstream SkillSpector without its LLM stage reached about 25% IDR on this set.
- **The fork rules add about 5 points of static IDR.** They target the probe-style attacks in `docs/COVERAGE_AUDIT.md`, which are more explicit than these context-blended injections.
- **With Sol, most injections are found and localised.** The bundle judge alone localises 61%, and the combined verdict blocks 14 of 18.
- **Caveat:** the Sol sample is small (18 cases), so it's a direction, not a precise rate. A full 180-case Sol run costs roughly 180 × 30–60k tokens.

## Likely-safe skills (benign, static)

| Verdict | Count |
|---|---|
| SAFE | 31 |
| CAUTION | 91 |
| DO NOT INSTALL | 8 (6.2%) |

- **51 of the 91 CAUTION verdicts have a score below 21.** For those, the CAUTION comes only from upstream's fail-closed rule: the skills reference files (`reference_missing`) that the dataset doesn't include. Counting only real findings, **37% of the safe skills are flagged and 6.2% are blocked.** For comparison, SkillTrustBench reports 42% false positives for upstream SkillSpector.
- **Most frequent HIGH rules on safe skills:** AE1 (11, incompletely inspected artifacts), P6 (7) and AR2 (5). The fork rules fire rarely: FG2 3 times and FG3 once.
- **A Sol run on 20 safe skills was started but stopped**, so it would not compete for the API key with an interactive test. To be rerun.

## Probes with the local Qwen2.5-Coder 7B
`eval/results/probes-qwen-coder-7b-strict.txt`, measured before the FG rules were added:
- 3 of 28 probes produced no finding, and 9 ended with a SAFE verdict.
- For comparison, Sol missed 1 probe and left 2 at SAFE (`docs/COVERAGE_AUDIT.md`).

Local 7B models are a useful free first pass, not a judge.
