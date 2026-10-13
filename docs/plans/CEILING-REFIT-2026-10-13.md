# Ceiling Refit — what SHOULD the confidence ceiling be? (Item 1)

**Date:** 2026-10-13 · **Agent:** Gilfoyle · **Repo:** `prototypes/weather-engine-source`
**Directive (Dan):** *"Is there a way to raise the ceiling? I'd prefer we raise the ceiling if possible."*

---

## The question

`core/intraday/multi_forecaster_fusion.py:318` caps the fused decision probability:

```
ceiling = min(MAX_CEILING, CALIBRATION_FLOOR + (MAX_CEILING-CALIBRATION_FLOOR)
              * min(1, resolution / RESOLUTION_FULL_SKILL))
```

`CALIBRATION_FLOOR=0.75`, `MAX_CEILING=0.99`, `RESOLUTION_FULL_SKILL=0.05`, default
`HOLDOUT_RESOLUTION=0.0004` (GEFS-step-24) → ceiling 0.75.

The brief's hypothesis: the 0.75 floor was fed GEFS-step-24's near-zero resolution, but the
*decision-time* signal (rate_1h) has resolution ~0.086 (>0.05), which would grant 0.99 — so
the ceiling could be raised. **That hypothesis does not survive contact with the data.**

---

## Finding 1 — the decision-time resolution is real, and σ-robust… but it is LOOK-AHEAD

rate_1h as scored by the diversity study is a **window-max** statistic: `build_grid` takes the
*best* p over the intraday scan `13:00 … 16:45` — i.e. it looks **past** the 13:30 pin. We
bounded the scan and re-scored (T anchored at the pin, strict strike-crossing, canonical truth):

| rate_1h scan | n | Brier | reliability | **resolution** | ceiling(formula) |
|---|---:|---:|---:|---:|---:|
| study window-max 13:00→16:45 | 14853 | 0.1072 | 0.0230 | **0.1046** | **0.99** |
| to 15:00 (13:00→15:00) | 14434 | 0.1335 | 0.0337 | 0.0912 | 0.99 |
| **bounded ≤ pin (13:00→13:30)** | 14040 | 0.3293 | 0.1542 | **0.0160** | **0.8268** |
| single point @13:30 | 13951 | 0.3386 | 0.1701 | 0.0223 | 0.857 |

**79–85% of rate_1h's "resolution" is information from after the decision instant.** Bounded to
what is knowable at 13:30, resolution collapses to **0.016** → ceiling **0.83**, barely above the
0.75 floor. (The bounded σ-sweep confirms: resolution 0.0095–0.0176 across σ=0.75–4.0, so this
is the *bound*, not the fixed-σ mapping.)

This is Workstream I's caveat in a sharper form: the resolution was inflated, but by **look-ahead
inside the day**, not (only) by the fixed σ. The pooled *single-point* p is near-degenerate
(Brier 0.49 — the nowcast is cold at 13:30, when the high is usually later), which is exactly
why the study scanned forward.

## Finding 2 — rate_1h is not even a bridge member

The live bridge fuses **p_gefs / p_analog / p_trajectory / p_market**
(`sbox_intraday_ev_bridge.build_fused_p_fn`; `DEFAULT_WEIGHTS`). `rate_1h` appears in the bridge
only in a comment. So even the bounded 0.83 is a proxy for a signal the book does not trade.
The bridge's only full-population member is the NWP path (GEFS pressure):

| decision signal | n | Brier | reliability | resolution | ceiling |
|---|---:|---:|---:|---:|---:|
| **gefs_p (bridge NWP member)** | 2740 | 0.3290 | 0.1631 | **0.0038** | **0.768** |
| rate_1h, bounded to pin (proxy) | 14040 | 0.3293 | 0.1542 | 0.0160 | 0.827 |
| rate_1h, study window-max | 14853 | 0.1072 | 0.0230 | 0.1046 | 0.99 |
| GEFS-step-24 holdout (current default) | 1899 | 0.2496 | 0.0002 | 0.0004 | 0.752 |

The bridge's disclosed member sits essentially **at the floor** (0.768 ≈ 0.75). The honest
decision-time evidence does **not** justify 0.90 or 0.99.

---

## Recommended ceiling per station

Two honest options, both far below the brief's hoped-for 0.99:

- **Keep 0.75 (status quo)** — defensible: the bridge's own member resolution is ~0.004.
- **Raise modestly to 0.80** — defensible: the *bounded* decision-time proxy (rate_1h) and the
  price-side member both sit at resolution ~0.016–0.022 → formula 0.83–0.86; a pooled 0.80
  banks most of that while staying well inside the look-ahead-free envelope.

**Do NOT raise to 0.90/0.99** — that requires the window-max number, which is look-ahead.

Per-station, from the **bounded** rate_1h (≤ pin) — the richest per-station decision-time
evidence available. These are *upper* bounds on what each station's ceiling could honestly be;
the formula value should be read as a cap, not a target:

| Station | n | bounded resolution | formula ceiling | recommended (pooled 0.80 cap) |
|---|---:|---:|---:|---:|
| KATL | 752 | 0.0070 | 0.7836 | **0.784** |
| KAUS | 615 | 0.0102 | 0.7990 | **0.799** |
| KBOS | 723 | 0.0082 | 0.7894 | **0.789** |
| KDCA | 606 | 0.0117 | 0.8062 | **0.800** |
| KDEN | 788 | 0.0135 | 0.8148 | **0.800** |
| KDFW | 581 | 0.0108 | 0.8018 | **0.800** |
| KHOU | 812 | 0.0188 | 0.8402 | **0.800** |
| KLAS | 544 | 0.0049 | 0.7735 | **0.774** |
| KLAX | 748 | 0.0244 | 0.8671 | **0.800** |
| KMDW | 798 | 0.0196 | 0.8441 | **0.800** |
| KMIA | 831 | 0.0258 | 0.8738 | **0.800** |
| KMSP | 444 | 0.0233 | 0.8618 | **0.800** |
| KMSY | 732 | 0.0319 | 0.9031 | **0.800** |
| KNYC | 750 | 0.0171 | 0.8321 | **0.800** |
| KOKC | 707 | 0.0134 | 0.8143 | **0.800** |
| KPHL | 878 | 0.0202 | 0.8470 | **0.800** |
| KPHX | 643 | 0.0049 | 0.7735 | **0.774** |
| KSAT | 629 | 0.0038 | 0.7682 | **0.768** |
| KSEA | 754 | 0.0229 | 0.8599 | **0.800** |
| KSFO | 705 | 0.0212 | 0.8518 | **0.800** |
| **pooled** | 14040 | 0.0160 | 0.8268 | **0.80** |

**Recommended: pooled ceiling 0.80**, per-station `min(0.80, bounded_formula)` → 0.80 everywhere
except KATL 0.784, KBOS 0.789, KAUS 0.799, KLAS 0.774, KPHX 0.774, KSAT 0.768. This is a
**modest, honest raise from 0.75 → 0.80** (+0.05), *not* the 0.99 the window-max number implied.

---

## Bottom line

- **A raise is possible, but a small one.** The honest decision-time ceiling is **~0.77–0.83**,
  pooling to **0.80**. Recommend **0.75 → 0.80**.
- **The case for 0.99 was look-ahead.** rate_1h's resolution is 0.087 only because the study
  scans to 16:45; bounded to the 13:30 pin it is 0.016. 79–85% of the "skill" arrives after the
  decision instant.
- **The bridge's own member (gefs) is at the floor** (resolution 0.0038 → 0.768). The 0.75 floor
  is roughly honest; 0.80 banks what little real, bounded skill exists.
- **Wiring is a separate decision** — nothing in `multi_forecaster_fusion.py` is changed here.

> Artifact: `artifacts/ceiling-refit/ceiling_refit_2026-10-13.json` ·
> Script: `scripts/ceiling_refit_2026-10-13.py`
