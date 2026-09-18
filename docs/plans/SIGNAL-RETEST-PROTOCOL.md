<!-- provenance=transcript-reconstruction | recovered 2026-09-18 | NOT byte-identical | content reconstructed from session transcript write() tool call -->
# SIGNAL-RETEST-PROTOCOL.md

## D.10 — Preregistered Retest Protocol

All retests must preregister thresholds BEFORE execution. This prevents p-hacking,
cherry-picking, and moving goalposts.

---

### Thresholds

| Experiment | Metric | Threshold | Sample | CI | Decision |
|------------|--------|-----------|--------|-----|----------|
| **D.2 UWC fusion** | Directional accuracy | ≥ LOOP - 0.02 (within 2pp of LOOP) | 20 stations, min 30 days each | 95% bootstrap | PASS = proceed with UWC as fusion default. FAIL = keep LOOP. |
| **D.4 NWP analog** | Directional accuracy | ≥ 0.55 | 5 stations min (KNYC, KLAX, KATL, KMDW, KBOS), 90+ days | 95% CI lower > 0.50 | PASS = include as pool member. FAIL = **KILL permanently** — signal architecture doesn't work regardless of input. |
| **D.5 HRRR** | Directional accuracy | No threshold — exploratory. Report raw ± CI. | All available stations/dates | 95% bootstrap | Report only. Results inform intraday architecture. |
| **D.7 Dewpoint** | Directional accuracy | ≥ 0.55 | All stations, min 14 days | 95% CI lower > 0.50 | PASS = ADVANCE to further testing. FAIL = KILL permanently. |

### Multiple Comparison Correction

- **Bonferroni threshold:** p < 0.008 for 6 planned comparisons
- **Sequential testing:** D.2 first (fusion default), then D.4/D.5/D.7 (parallel)
- **Stopping rule:** No early stopping. Full sample required for each test.

### Standard Deviation Protocol

1. Preregister hypothesis, threshold, sample size, and stopping rule before computation
2. No peeking — do not compute results and decide thresholds after
3. Report ALL stations, not cherry-picked best
4. Bootstrap CIs (10,000 resamples, 95%) on all accuracy measures
5. Store full per-station breakdown, not just aggregates
6. Document any data exclusions with rationale
7. If a signal fails at its threshold, it is KILLED — no second chances without new data source

### Version

- Created: 2026-09-04
- Author: D.10 protocol
- Status: ACTIVE