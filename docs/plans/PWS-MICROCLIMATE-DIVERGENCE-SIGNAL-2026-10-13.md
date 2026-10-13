# PWS Microclimate Divergence — Signal Test (2026-10-13)

**Date:** 2026-10-13
**Lane:** Sprint 2 — Lane B (B3)
**Owner:** Gilfoyle
**Repo:** `prototypes/weather-engine-source/`
**Status:** **PARK — do not pre-register.** The divergence shows no robust
predictive lead over the ASOS 1h-forward rate shift.

---

## 0. Bottom line

`pws_microclimate_divergence = ASOS_tmpf[h] − median(nearby PWS tmpf[h])` is a
real **level** quantity (mean offset 0.5–1.0 °F, driven by siting/elevation),
but it does **not** predict a rate shift at hour h+1.

* Only **1 of 5** stations (KATL) reaches even a marginal same-hour association
  (raw r = 0.50, circular-shift null **p = 0.042**) — and that does **not**
  survive a multiple-comparison correction across 5 stations (Bonferroni
  threshold ≈ 0.01).
* The **lag sweep peaks at lag 0 for every station** (symmetric ±1/±2): the
  association is *contemporaneous co-movement*, not a lead.
* The one apparently-strong **negative** signal (KMIA, raw r = −0.21) is
  **mechanical**: it collapses to **r = −0.02** once ASOS[h] is partialled out,
  because `div[h]` and `fwd_rate[h]` both embed the shared `ASOS[h]` term.

Consistent with the original trajectory finding (**divergence is a level
phenomenon, not a rate one**), the correct disposition is to **park** this as a
signal. The level offset may still be useful as a **de-bias input** to other
signals — not as its own rate predictor.

---

## 1. Method

* **Feed:** MADIS LDAD mesonet NetCDF (free, keyless, hourly) — 123 files,
  `2026-10-08 07:00 … 2026-10-13 09:00` (122 hourly slots).
* **Targets:** KLAX, KATL, KDEN, KSEA, KMIA (as specified).
* **Nearby PWS:** within 5 mi (12 mi fallback), ≥50% hour coverage; robust
  **median** used. Picked: KLAX 11, KATL 11, KDEN 18, KSEA 7, KMIA 6.
* **ASOS level series:** `data/iem_asos_1min.db` hourly mean `tmpf` (production
  source).
* **Signal (level):** `div[h] = ASOS[h] − median(PWS[h])`; anomaly
  `div_anom[h] = div[h] − mean(div)` (kills the constant siting offset).
* **Predictive target:** forward rate `fwd[h] = ASOS[h+1] − ASOS[h]`.
* **Guards added after a first pass looked promising:**
  1. **Partial correlation** of `div_anom[h]` vs `fwd[h]` **controlling for
     ASOS[h]** — mandatory because `div[h]` and `fwd[h]` share `ASOS[h]`.
  2. **Circular-shift null** (500 shifts) — `p(|r_shift| ≥ |r_obs|)`; ~120
     overlapping hourly samples are autocorrelated, so a raw r ≈ 0.3 is not
     significant on its own.
  3. **Lag sweep** k ∈ {−2,−1,0,+1,+2} — a predictive lead must peak at k≠0.

---

## 2. Results

| Station | PWS | mean div (°F) | sd div | raw r (div_anom~fwd) | partial r (ctrl ASOS) | shift-null p | dir-agree | lag peak |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| KLAX | 11 | +1.03 | 1.12 | 0.220 | 0.210 | 0.127 | 56.3% | **0** |
| KATL | 11 | −0.50 | 1.42 | **0.497** | 0.501 | **0.042** | 67.7% | **0** |
| KDEN | 18 | −0.94 | 2.49 | 0.299 | 0.320 | 0.407 | 66.7% | **0** |
| KSEA | 7 | +0.95 | 1.90 | −0.104 | −0.129 | 0.653 | 50.6% | **0** |
| KMIA | 6 | +0.51 | 1.29 | −0.208 | **−0.019** | 0.644 | 41.7% | **0** |

Pooled mean raw r across stations = **0.141**.

### Interpretation of each guard
* **Partial vs raw:** KMIA's raw −0.208 → partial **−0.019**: the association
  was entirely the shared `ASOS[h]` term. KLAX/KATL/KDEN are stable under
  partialling, so *their* r is not purely mechanical — but it is still
  always positive and always strongest at lag 0.
* **Shift null:** only KATL clears p<0.05; with 5 stations the family-wise
  threshold is ≈0.01, so KATL is **not** significant after correction.
* **Lag peak at 0 everywhere:** the divergence and the ASOS rate are
  co-incident. There is **no lead** at h→h+1 — the question posed by the task.
* **Negative r at KSEA/KMIA** is the wrong sign for a "PWS leads ASOS" story and
  does not survive the null.

---

## 3. Why this is the expected result

The trajectory test (`scripts/pws_trajectory_test_2026-10-13.py`) already found
divergence is a **level** property: a PWS's mean offset from its ASOS is a
fixed siting/elevation bias, and removing it (de-biased variant) did not improve
the rate signal. This test asked the sharper question — *does a growing
divergence predict a rate shift next hour?* — and the answer is **no**: the
level and the rate move together at the same hour (if at all), not lead/lag.

---

## 4. Disposition

**PARK.** Do not add `pws_microclimate_divergence` to the signal family and do
not pre-register it. Not all ideas work; this one doesn't clear the bar.

**Forward use (non-signal):** the per-station **level offset** is a stable
de-bias quantity. If a future task wants a PWS-informed *bias correction* (as an
input to fusion, not a rate predictor), that is a separate, level-side design —
not this signal.

---

## 5. Reproduce / artifacts

```
.venv-gefs/bin/python scripts/pws_microclimate_divergence_2026-10-13.py all
```

* Script: `scripts/pws_microclimate_divergence_2026-10-13.py`
* Result JSON: `artifacts/pws-divergence/pws_microclimate_divergence_2026-10-13.json`
* MADIS cache (`data/_pws_madis_cache/`, ~13 MB) **deleted after analysis**
  per the resource constraint.

---

## 6. Do-not-wire

Nothing wired. No `trades` write. No signal registration. No live-book change.
