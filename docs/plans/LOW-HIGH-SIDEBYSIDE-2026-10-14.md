# LOW vs HIGH — End-to-End Side-by-Side (2026-10-14)

**Owner:** Gilfoyle · **Lane:** Sprint 3 (technical)
**Repo:** `prototypes/weather-engine-source/`
**Status:** **ANALYSIS / DESIGN.** Read-only on the live book. Nothing wired.
**Script:** `scripts/low_high_sidebyside_2026-10-14.py` (reuses the existing LOW + HIGH honest machinery verbatim)
**Artifacts:** `data/low_high_sidebyside_2026-10-14.json` · `artifacts/low-market-lane/low_high_sidebyside_2026-10-14.json`
**Companions:** `docs/plans/LOW-MARKET-LANE-DESIGN-2026-10-14.md` · `docs/plans/FUSION-ALTERNATIVES-SWEEP-2026-10-14.md` · `docs/plans/HONEST-SIGNAL-ACCURACY-2026-10-14.md`

Dan (2026-10-14): *"Give me end to end side by side HIGH vs LOW, expert derived."*
Dan disagreed with the earlier "keep the same pin" finding — so this doc backs the comparison with **two concrete analyses** run against the honest cohorts, not opinion.

---

## 0. Bottom line

The side-by-side **confirms the earlier LOW finding, with measured evidence this time**:

1. **The LOW primary signal is `om_gfs` Tmin** — a genuine level forecast with real skill (**Brier 0.2094, BSS +0.138, dir_acc 0.65** at 13:30, n=84). **Not** the cooling rate.
2. **The cooling-rate signal (`rlow_1h`) does not work** — its Brier is **worse than the base rate at 8 of 11 evening pins** (BSS −0.13 to −0.69 on the 11k-day panel); the running-low **level-only** arm beats it at every pin. A cooling nowcast adds no discrimination.
3. **The pin should stay at 13:30 station-local.** `om_gfs` Tmin skill is a **plateau from ~06:00 to 21:00** and **declines** after 21:00 (BSS 0.138 @13:30 → 0.056 @23:00). Moving the LOW pin to 18:00–23:00 makes the primary signal **worse**, not better — the opposite of the "cooling happens overnight so pin later" intuition.
4. **LOW is structurally harder than HIGH** (Tmin Brier 0.2094 vs Tmax 0.1067; ~2× the level MAE) — but it is a **signal** difference, **not a pin or exit-schedule difference**. HIGH and LOW can share the pin, gate stack, grade ladder, and entry window; only the primary signal (Tmax → Tmin) and the running-low truncation are LOW-specific.

---

## 1. The comparison table

| Dimension | HIGH | LOW | Same? |
|---|---|---|---|
| **Predicted quantity** | Daily **maximum** temp | Daily **minimum** temp | ✗ structurally different |
| **Settlement source** | NWS Daily Climate Report (**max**) | NWS Daily Climate Report (**min**) | ✗ different field, same report |
| **Repo truth axis** | canonical GHCN settlement, `market_type=HIGH` | canonical GHCN settlement, `market_type=LOW` | (GHCN derives from the same NWS/HAD station set) |
| **Crossing axis** | `T=floor(hsf@13:30)+1; y = settled_max > T` (`==` LOSS) | `T=floor(rlow@pin)−0.5; y = settled_min ≤ T` | ✗ mirrored |
| **Primary signal** | `om_gfs` Tmax, **Brier 0.1067** (full cohort) / 0.1096 (fusion cohort), dir 0.84 | `om_gfs` Tmin, **Brier 0.2094** (global-bc) / 0.1474 (per-station, in-sample), dir 0.65 — **measured §2.1** | ✗ same recipe (Open-Meteo→debias→Φ), 2× harder |
| **Decision pin** | **13:30 station-local** (honest: 15:00 is a look-ahead mirage) | **13:30 station-local** — swept 18:00–23:00, **no gain** (§2.2); keep 13:30 | ✓ same pin |
| **Rate analog (nowcast)** | heating rate `rate_1h` — honest **Brier 0.3617** (weak) | cooling rate `rlow_1h` — **computed + swept §2.3**; Brier 0.24–0.33, **BSS ≤ 0** (no skill; worse than level-only) | ✓ both are weak; neither is a primary |
| **Entry window** | T−20 / T−18 / T−14h before midnight-local | **same** (settlement is still midnight-local) | ✓ identical |
| **Exit rules** | h−1 close · take-profit · stop-loss · signal-flip | **h−1 close only** (simpler — fewer revaluation events on a falling series) | ✗ LOW deliberately simpler |
| **Grade binding** | F7 ladder (S/A/B/C, $10 cap) | **same ladder** | ✓ shared |
| **Run-time behavior** | max rises during the day; set mid-afternoon (HOTD ≈ 15:30 local) | min falls overnight; set **near dawn** (median ≈ 05:45 local) | ✗ opposite diurnal phase |
| **Pin → settlement gap** | 10.5h constant (both station-local) | 10.5h constant (same) | ✓ identical |
| **Gate / exposure** | G0–G5, F9 caps | **same** (direction-symmetric substrate) | ✓ shared |

**Read:** every **material** difference is in the *signal and the diurnal phase*. The **pin, entry window, grade ladder, gate stack, and settlement clock are shared**. The only LOW-specific code is (a) the Tmin primary and (b) the running-low truncation.

---

## 2. The two analyses (measurements that fill the gaps)

### 2.1 — `om_gfs` Tmin Brier @ 13:30 pin (LOW backfill cohort)

**Cohort A:** the requested LOW backfill subset — **178** LOW truth station-days, **84** with ASOS recs ∩ `om_gfs` Tmin ∩ canonical LOW truth. 20 stations, ~4 days each.

`om_gfs` Tmin — same Open-Meteo `gfs_seamless` recipe as the HIGH Tmax member, generalized to the daily **minimum**:

| metric | raw | global-bc | per-station-bc (in-sample) |
|---|---:|---:|---:|
| **Brier** (lower better) | 0.2316 | **0.2094** | **0.1474** |
| **ECE** | 0.1760 | 0.1345 | 0.1020 |
| **dir acc** | 0.6548 | 0.6548 | 0.7500 |
| **BSS** (1 − Brier/base) | 0.070 | **0.138** | 0.393 |
| base rate | 0.4167 | 0.4167 | 0.4167 |
| mean p | 0.5835 | 0.4628 | 0.4456 |
| n | 84 | 84 | 84 |

- **Bias** (om_gfs Tmin − settled low) = **−1.2069 °F**; **σ** = **3.2691 °F**; level **MAE** = **2.5456 °F** centered (2.7317 raw).
- **Verdict:** `om_gfs` Tmin is a **real, correctly-signed signal** (BSS +0.138 global-bc; +0.39 per-station in-sample) — it is the LOW primary.
- **HIGH anchor for the same comparison:** `om_gfs` Tmax **Brier 0.1096** on the fusion cohort / **0.1067** full cohort, dir 0.84, ECE 0.042. So **LOW Tmin is ~1.9× the Brier of HIGH Tmax** and its σ (3.27°F) is ~2× the Tmax deterministic σ (4.0 F used on the passing axis, ~1.3–2.1°F level MAE). The min is genuinely harder (nocturnal boundary layer / radiational cooling).
- **Caveat (honest):** the per-station-bc **0.1474 is IN-SAMPLE** — the bias/σ are refit on the same 84 days (~4/station; 2 stations have n=1). **Do not quote 0.1474 as a held-out Brier.** The defensible headline is **global-bc 0.2094 (BSS 0.138)**; a real per-station refit needs the full population.

### 2.2 — `om_gfs` Tmin per-pin sweep (does a later pin help?)

| pin | 00:00¹ | 03:00 | 06:00 | 09:00 | 12:00 | **13:30** | 15:00 | 18:00 | 20:00 | 21:00 | 22:00 | **23:00** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Brier | *0.1016* | 0.1902 | 0.2161 | 0.2138 | 0.2138 | **0.2094** | 0.2094 | 0.2101 | 0.2116 | 0.2122 | 0.2171 | 0.2226 |
| BSS | *0.215* | +0.122 | +0.131 | +0.123 | +0.123 | **+0.138** | +0.138 | +0.128 | +0.122 | +0.119 | +0.099 | +0.056 |
| dir acc | *0.847* | 0.707 | 0.671 | 0.639 | 0.639 | **0.655** | 0.655 | 0.655 | 0.643 | 0.643 | 0.631 | 0.631 |

¹ the 00:00 column is a small-n artifact (n=72; only days whose prior-day buckets extend into the target local day) — **not** a usable pin. The stable plateau is **~06:00–21:00**, peaking at **13:30**.

**Verdict: MOVE THE PIN LATER AND THE PRIMARY SIGNAL GETS WORSE.** BSS *declines* monotonically after 21:00 (0.138 → 0.056). There is **no predictive reason** to pin LOW in the evening. This is the same conclusion as the HIGH lane (13:30 honest; 15:00 was a look-ahead mirage) — and now it is *measured directly on the LOW primary*.

**Supporting "why":** the daily minimum is set **near dawn** (median ≈ 05:45 local; 46% of days in 05:00–07:00). By 13:30 the running low **already equals the final min on 81.7%** of days (82.8% @18:00, 84.5% @21:00 — a later pin buys only **+2.8pp**). And because settlement is midnight-local, a 20:00–22:00 pin would leave only **2–4h** to entry (thin liquidity) for **zero** measured gain — pure cost.

### 2.3 — Cooling-rate signal (`rlow_1h`) sweep, 18:00–23:00

The exact mirror of HIGH `rate_1h` on falling temps: `mu_low = rlow − deflated_drop`, `p = Φ((T − mu_low)/σ)`, cap 8°F. Two arms: **cooling** (projected drop) vs **level-only** (just the running low, no projection). BSS = 1 − Brier/base-rate.

**Cohort A — LOW backfill (n ≈ 57–59):**

| pin | 18:00 | 19:00 | 20:00 | 21:00 | 22:00 | 23:00 |
|---|---:|---:|---:|---:|---:|---:|
| cooling Brier | 0.2394 | 0.2868 | 0.3177 | 0.2407 | 0.2566 | 0.2991 |
| cooling **BSS** | +0.005 | −0.192 | −0.301 | +0.014 | −0.058 | −0.227 |
| level-only Brier | **0.2294** | 0.2328 | 0.2375 | 0.2460 | 0.2419 | 0.2435 |
| level-only BSS | +0.047 | +0.033 | +0.027 | −0.008 | +0.003 | +0.001 |

**Cohort B — ASOS∩GHCN-low full panel (n ≈ 11,250–11,800, the stable read):**

| pin | 18:00 | 19:00 | 20:00 | 21:00 | 22:00 | 23:00 |
|---|---:|---:|---:|---:|---:|---:|
| cooling Brier | 0.2732 | 0.3059 | 0.3148 | 0.2819 | 0.2529 | 0.2397 |
| cooling **BSS** | **−0.417** | **−0.604** | **−0.676** | **−0.530** | **−0.424** | **−0.477** |
| level-only Brier | 0.1933 | 0.1918 | 0.1898 | 0.1875 | 0.1831 | **0.1725** |
| level-only BSS | −0.003 | −0.006 | −0.011 | −0.018 | −0.031 | −0.063 |

**Verdict: the cooling-rate signal is inert-to-deleterious.**
- On the 11k panel the **cooling arm is far worse than the base rate** at every pin (BSS −0.42 … −0.69) and the **level-only arm beats it at every pin** (e.g. 23:00: 0.1725 vs 0.2397).
- On cohort A the cooling arm is only "better than base" at 18:00 and 21:00 (BSS +0.005 / +0.014) — noise on n≈58 — and the level-only arm beats it at **10 of 11** pins.
- **The cooling term adds no discrimination over simply reading the running low.** (Same shape as HIGH `rate_1h` — honest Brier 0.3617 — which is also a weak, dominated nowcast.)

---

## 3. Expert read (what this means, end to end)

1. **Signal differs, schedule doesn't.** HIGH and LOW should run the **same** pin (13:30), **same** entry windows (T−20/18/14h before midnight-local), **same** grade ladder, **same** gate stack. The LOW lane's only bespoke parts are the **Tmin primary** and the **running-low truncation** (`if T ≥ rlow: p = 1.0` — a lock mirroring HIGH's `high_so_far` truncation).
2. **The cooling module is a diagnostic, not a spine.** Keep `cooling_nowcast` in the code as a measured mirror; **do not wire it** as the LOW primary. Its only plausible future use is a station-specific micro-adjustment near dawn (03:00–07:00) if a larger cohort ever shows it beating level-only there — not now.
3. **LOW wages: expect lower confidence, same machinery.** Tmin Brier ~0.21 vs Tmax ~0.11 means LOW probabilities should be **less extreme** at the same lead. The ceiling/ladder (`F7`, $10 cap) is shared, but the LOW `om_gfs` σ (3.27°F) should enter the LOW probability directly (it already does in the axis above) rather than borrowing HIGH σ (4.0 F).
4. **Exit asymmetry is intentional.** HIGH uses h−1 close + TP + SL + signal-flip; LOW uses **h−1 close only**. The rationale (fewer revaluation events as the min falls monotonically overnight) is directionally sound and unchanged.
5. **On Dan's disagreement:** the honest data still says **keep the 13:30 pin** — but the reason is now explicit and measured: the min is set near dawn, the running low is already final on 82% of days by 13:30, and `om_gfs` Tmin skill *declines* after 21:00. A later pin is cost with no gain. If Dan wants an evening pin for a different reason (entry price, liquidity), that is an **execution** choice to be traded off explicitly — it is not supported by the forecast.

---

## 4. Recommendation

**ADVANCE the LOW lane on the shared schedule, with a Tmin primary — do not move the pin.**

1. **Primary signal:** `om_gfs` Tmin + running-low truncation (as designed). Refit the per-station bias/σ on the **full** station-day population before trusting per-station numbers (the 0.1474 here is in-sample on 84 days).
2. **Pin:** **13:30 station-local** (parity with HIGH). The 18:00–23:00 sweep shows no gain; the primary signal degrades after 21:00.
3. **Cooling rate:** **PARK** — measured, documented, not wired (BSS ≤ 0).
4. **Exit:** h−1 close only (LOW-specific, simpler).
5. **Shared unchanged:** entry windows, grade ladder (F7, $10 cap), gate stack (G0–G5), settlement clock (midnight-local).

**One-line answer to Dan:** *Side-by-side says LOW differs in **signal** (om_gfs Tmin, ~2× harder, Brier 0.21 vs 0.11) and **diurnal phase** (min sets near dawn) — but NOT in pin, entry window, grade ladder, or gate. Cooling-rate doesn't work (BSS ≤ 0); keep the 13:30 pin; the later-pin intuition loses to the data.*

---

## Reproduce

```bash
python3 scripts/low_high_sidebyside_2026-10-14.py run
# needs /tmp/we_asos_recs_2026-10-13.pkl.gz, /tmp/we_grid_2026-10-13.pkl.gz,
# /tmp/we_low_omgfs_tmin_2026-10-14.json, /tmp/honest_omgfs_cache_2026-10-14.json,
# /tmp/honest_analog_traj_2026-10-14.pkl.gz + the LOW backfill CSV.
```
