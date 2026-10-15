# Honest Calibration Audit — external red-team of `honest_calibrated_p`

**Date:** 2026-10-15 · **Owner:** Gilfoyle (external audit) · **Repo:** `prototypes/weather-engine-source/`  
**Script:** `scripts/honest_calibration_audit_2026-10-15.py` · **Artifact:** `data/honest_calibration_audit_2026-10-15.json`

**Audited transform (shipped):**

```
calibrated_p = clamp_to[0.01, ce]( 0.5 + (p_fused - 0.5) * (1 - ECE/2) )
               ECE = 0.47 (constant)   ce = per-station ceiling (pooled 0.84 fallback)
```

## VERDICT — **REPAIR**

- the shipped transform DEGRADES the shipped signal
- the shrink constant is measured from a DIFFERENT (skill-less) signal
- it loses to raw at 14/20 stations
- None raises TypeError, not ValueError (breaks the documented fail-closed contract)
- a NaN ceiling silently becomes the MAX ceiling (fail-OPEN)

## 1. Neutrality (the one-sided bug)

`honest_calibrated_p(0.5, ce)` over all ceilings: 
`{"ce=0.5": 0.5, "ce=0.75": 0.5, "ce=0.84": 0.5, "ce=0.9031": 0.5, "ce=0.999": 0.5}`  
**Neutral at 0.5 by construction — the old 0.5705 one-sided bug is fixed.**

Symmetric about 0.5 at every probe point: **True**.

## 2. Edge cases / fail-closed contract

| input | result | raised |
|---|---:|---|
| `p=0.01,ce=0.84` | 0.1251 | — |
| `p=0.50,ce=0.84` | 0.5 | — |
| `p=0.99,ce=0.84` | 0.84 | — |
| `p=NaN,ce=0.84` | None | ValueError |
| `p=None,ce=0.84` | None | TypeError |
| `p=0.7,ce=NaN` | 0.653 | — |
| `p=0.7,ce=None` | None | TypeError |
| `p=0.7,ce=0.2(<0.5)` | 0.5 | — |
| `p=0.7,ce=1.5(>1)` | 0.653 | — |
| `p=-0.3` | 0.01 | — |
| `p=1.7` | 0.84 | — |

The docstring promises *"Raises ValueError on a non-finite `p_fused` (callers fail closed)"*. NaN does raise `ValueError`. **`None` raises `TypeError`**, and a **NaN ceiling silently clamps to the MAX ceiling** — both escape the documented fail-closed contract.

## 3. Murphy decomposition on the CALIBRATED output

### Cohort A — sole om_gfs (the shipped HIGH path), n = 2927

| variant | n | Brier | reliability | resolution | uncertainty | ECE | mean_p | base |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| raw om_gfs_bc_ps | 2927 | **0.1067** | 0.0047 | 0.0494 | 0.1524 | 0.0376 | 0.8019 | 0.8124 |
| shipped calibration | 2927 | **0.1206** | 0.0151 | 0.0463 | 0.1524 | 0.1147 | 0.6977 | 0.8124 |

- base rate **0.8124**, mean raw p **0.8019**, mean calibrated p **0.7309**
- raw gap to base **-0.0106** → calibrated gap **-0.0815**  
The shrink pulls the mean probability *away* from the base rate: the transform manufactures **under**confidence out of a signal that was already close to calibrated.

### Cohort B — fused 5-member (bridge path), n = 2296

| variant | n | Brier | reliability | resolution | uncertainty | ECE | mean_p |
|---|---:|---:|---:|---:|---:|---:|---:|
| raw (pre-ceiling) | 2296 | **0.1459** | 0.0101 | 0.0172 | 0.1549 | 0.0868 | 0.8293 |
| raw (legacy clamp) | 2296 | **0.1523** | 0.0106 | 0.0133 | 0.1549 | 0.0942 | 0.7204 |
| shipped calibration | 2296 | **0.1658** | 0.022 | 0.0089 | 0.1549 | 0.1452 | 0.6686 |

### Reliability diagram (10 bins) — calibrated sole cohort

| bin | n | mean p | empirical rate | gap |
|---|---:|---:|---:|---:|
| 0.0-0.1 | 0 | None | None | None |
| 0.1-0.2 | 88 | 0.1569 | 0.2841 | -0.1271 |
| 0.2-0.3 | 117 | 0.253 | 0.3675 | -0.1145 |
| 0.3-0.4 | 121 | 0.3529 | 0.3967 | -0.0438 |
| 0.4-0.5 | 169 | 0.4499 | 0.503 | -0.0531 |
| 0.5-0.6 | 171 | 0.5533 | 0.5673 | -0.0139 |
| 0.6-0.7 | 218 | 0.6498 | 0.6881 | -0.0383 |
| 0.7-0.8 | 952 | 0.7717 | 0.9181 | -0.1464 |
| 0.8-0.9 | 1091 | 0.8334 | 0.9679 | -0.1345 |
| 0.9-1.0 | 0 | None | None | None |

## 4. Is the ECE = 0.47 constant still valid?

| signal | observed ECE on this cohort |
|---|---:|
| rate_1h_pit (source of the 0.47 constant) | 0.4689 |
| om_gfs_bc_ps (shipped sole signal) | 0.0376 |
| fused_5 pre-ceiling | 0.0868 |

The 0.47 constant is **rate_1h's** ECE — a signal with **no skill** on this axis (Brier ≈ 0.36, dir-acc ≈ 0.20, i.e. worse than a coin flip). The shipped signal's own ECE is ~12× smaller. The transform therefore shrinks the *good* signal by the *broken* signal's unreliability.

| shrink under | ECE used | factor (1−λ) | 0.90 → | Brier (sole) |
|---|---:|---:|---:|---:|
| constant ECE (0.47, shipped) | 0.47 | 0.765 | 0.806 | 0.1206 |
| renormalized to om_gfs (shipped signal) | 0.0376 | 0.9812 | 0.8925 | 0.123 |
| renormalized to fused | 0.0868 | 0.9566 | 0.8826 | 0.1227 |

## 5. Ceiling leveling

- 20 stations carry a custom ceiling; range **0.7682–0.9031** (mean 0.8253), 19 distinct values
- all within [0.75, 0.90]: **False**  
- Per-station values **are** distinct and **are** looked up per station — this is *not* a single constant reused everywhere. **But** the refit formula is `0.75 + 0.24·min(1, resolution/0.05)` where `resolution` is **rate_1h's** — the skill-less signal. So a per-station *number* is leveled, but it is leveled off the wrong signal's noise + a 0.75 floor.
- the ceiling binds on **1555/2927** sole-cohort rows (53.1%)

## 6. Per-station cross-validation (calibrated vs raw)

- calibrated beats raw at **6/20** stations
- **beats every station: False**

| station | n | Brier raw | Brier calibrated | Δ | calibrated better | ceiling |
|---|---:|---:|---:|---:|:--:|---:|
| KATL | 149 | 0.0547 | 0.0883 | +0.0335 | **NO** | 0.7836 |
| KAUS | 139 | 0.019 | 0.0555 | +0.0364 | **NO** | 0.799 |
| KBOS | 153 | 0.1691 | 0.1704 | +0.0013 | **NO** | 0.7894 |
| KDCA | 104 | 0.0826 | 0.1038 | +0.0212 | **NO** | 0.8062 |
| KDEN | 153 | 0.0692 | 0.0877 | +0.0185 | **NO** | 0.8148 |
| KDFW | 152 | 0.0479 | 0.075 | +0.0271 | **NO** | 0.8018 |
| KHOU | 152 | 0.1865 | 0.1814 | -0.0050 | yes | 0.8402 |
| KLAS | 148 | 0.0419 | 0.0789 | +0.0369 | **NO** | 0.7735 |
| KLAX | 134 | 0.2048 | 0.1972 | -0.0076 | yes | 0.8671 |
| KMDW | 152 | 0.1539 | 0.1544 | +0.0005 | **NO** | 0.8441 |
| KMIA | 145 | 0.2222 | 0.2102 | -0.0120 | yes | 0.8738 |
| KMSP | 156 | 0.053 | 0.0677 | +0.0147 | **NO** | 0.8618 |
| KMSY | 150 | 0.179 | 0.1754 | -0.0036 | yes | 0.9031 |
| KNYC | 150 | 0.1776 | 0.1733 | -0.0043 | yes | 0.8321 |
| KOKC | 151 | 0.052 | 0.0757 | +0.0237 | **NO** | 0.8143 |
| KPHL | 150 | 0.0973 | 0.1079 | +0.0106 | **NO** | 0.847 |
| KPHX | 140 | 0.0346 | 0.0731 | +0.0385 | **NO** | 0.7735 |
| KSAT | 148 | 0.0309 | 0.076 | +0.0451 | **NO** | 0.7682 |
| KSEA | 153 | 0.044 | 0.0567 | +0.0128 | **NO** | 0.8599 |
| KSFO | 148 | 0.2112 | 0.2047 | -0.0065 | yes | 0.8518 |

## Recommendation

**REPAIR.**

1. **Set the shrink constant from the SHIPPED signal, not rate_1h.** Recompute ECE on the signal the transform is actually applied to (om_gfs_bc_ps ≈ 0.04, not 0.47). Under the current constant the transform is a 23.5% one-size-fits-all pull toward 0.5 with no basis in the shipped signal's miscalibration.
2. **Stop treating the output as a probability.** A fixed linear shrink + a clamp is not a calibration. Either fit an isotonic/Platt map on a holdout (the machinery already exists: `core/platt_calibration.py`) or drop the shrink and keep the ceiling clamp.
3. **Re-derive the ceilings from the shipped signal's resolution.** The current ceilings inherit rate_1h's (near-zero) resolution over a 0.75 floor.
4. **Fix the fail-closed contract:** reject `None` with `ValueError`, and reject a non-finite `ceiling` instead of silently clamping it to 0.999.

## Reproduce

```bash
python3 scripts/honest_calibration_audit_2026-10-15.py
```

