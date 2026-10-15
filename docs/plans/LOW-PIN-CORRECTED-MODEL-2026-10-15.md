# LOW pin — CORRECTED target model (Dan's challenge, 2026-10-15)

**Status:** analysis complete (read-only). **Do NOT arm `LOW_BRIDGE_ENABLED`.**
**Script:** `scripts/low_pin_corrected_model_2026-10-15.py`
**Artifact:** `data/low_pin_corrected_2026-10-15.json`
**Supersedes the framing of:** `docs/plans/LOW-PIN-SWEEP-MARKET-TIMING-2026-10-14.md` (AG-1)

---

## Why this exists — Dan was right at the framing level

Dan (2026-10-15):

> "The low market is dead in that time window because the low of the day is going
> to happen overnight. I think you're presuming that the target is during the day."

The prior LOW pin work treated the LOW like the HIGH: a daytime decision point
where a still-forming extreme is forecast. **That is the wrong target model.**

The daily minimum forms **overnight**. The running-low argmin histogram on this
cohort (84 station-days, ASOS bucket means) peaks at **05:00–07:00 local**:

```
hour:  00  01  02  03  04  05  06  07  08..20  21  22  23
 n:     3   5   4   6   3  21  20   9    0..1   1   3   8
```

So a **13:30-local pin is AFTER the event on 84.5% of days** — the running low has
already equalled the final min. The market price there is mostly *settled
information*, not a forecast. The weather forecast only has work to do on the
minority of days where the low has **not** formed yet.

## Method (per candidate pin)

For pins 13:30 (incumbent), 15:00, 17:00, 19:00, 20:00, 21:00, 22:00, 23:00 local:

| # | Measure | Source |
|---|---|---|
| (a) | **Certainty fraction** — % of station-days where the low has already formed (argmin bucket ≤ pin) | `/tmp/we_asos_recs` bucket means |
| (b) | **om_gfs Tmin Brier on UNCERTAIN days only** — the forecast matters exactly here | om_gfs Tmin cache, bias-corrected, running-low truncation |
| (c) | **median spread + quote depth** at the pin hour | `kalshi_market_snapshots.db` KXLOWT* two-sided quotes |
| (d) | **Realized decision edge** — one near-money contract per station-day; bet the side the calibrated model prices as underpriced; `EV = payoff − mid − cost` | quotes + **Kalshi's own recorded settlement** |

**Boundary constraint:** the entry window forbids entering within T−2h of
settlement (settlement = midnight station-local). For an EDT-style station
(settlement 04:00Z) the latest usable pin is **22:00 local**; 23:00 is reported
but flagged `OUT_OF_BOUNDS`.

### Two truth bugs found and fixed (do not regress these)

1. **Realized outcome must use Kalshi's OWN settlement, not METAR truth.**
   `strike_ladder.result` (Kalshi's recorded yes/no) disagrees with the
   METAR-derived `settlement_truth` on **~36%** of quoted cohort contracts; the
   derived settled minima differ by **~1.7 °F median**. Scoring the realized edge
   off the METAR temperature manufactures a **fake +$0.35–0.53/contract edge at
   every pin** (all signs positive = the tell). The realized edge below uses
   `strike_ladder.result`.
2. **Snapshot quotes must be date-aligned to the ticker's target day**
   (the snapshot's own station-local date == the ticker's target date). Without
   this a quote a full day early leaks in and the "near-money" contract is read
   before the market had the day's information.

### Market is well-calibrated; the om_gfs model is NOT

Calibration of the near-money contracts at 13:30 vs the Kalshi result:

| mid bin | n | realized YES |
|---|---|---|
| 0.2–0.3 | 36 | 0.25 |
| 0.3–0.4 | 13 | 0.46 |
| 0.4–0.5 | 16 | 0.44 |
| 0.5–0.6 | 5 | 0.80 |
| 0.7–0.8 | 9 | 0.89 |
| 0.9–1.0 | 86 | 1.00 |

The **market is calibrated** (monotone, close to the diagonal). The **om_gfs
model probability is badly miscalibrated on this cohort** — on contested
contracts (0.2–0.8 mid) at 13:30:

```
mean_mid = 0.576   mean_model_p = 0.144   realized_yes_rate = 0.526
Brier(market) = 0.223     Brier(model) = 0.436
```

The model is *worse than the market* and on the wrong side; its apparent
"+0.4 model edge" is overconfidence, not skill. **Any edge taken from the model
signal on this cohort is not real.**

## Per-pin table

| pin | certainty | Brier (uncertain only) | median spread | realized edge (Kalshi truth) | n | in-bounds |
|---|---|---|---|---|---|---|
| 13:30 | 0.845 | **0.1443** | 0.04 | −0.045 | 59 | yes |
| 15:00 | 0.845 | 0.1443 | 0.04 | −0.051 | 60 | yes |
| 17:00 | 0.857 | **0.1398** | 0.035 | **−0.039** | 58 | yes |
| 19:00 | 0.857 | 0.1623 | **0.030** | −0.044 | 55 | yes |
| 20:00 | 0.857 | 0.1623 | 0.04 | −0.102 | 51 | yes |
| 21:00 | 0.869 | 0.1814 | 0.05 | −0.083 | 49 | yes |
| 22:00 | 0.869 | 0.2189 | 0.06 | −0.122 | 49 | yes |
| 23:00 | 0.905 | 0.2616 | 0.05 | −0.050 | 48 | **NO** (past T−2h) |

Paired vs the 13:30 incumbent (same station-days), realized edge:

```
15:00 −13:30: mean diff = +0.002 (se 0.038)   ~0
19:00 −13:30: mean diff = −0.003 (se 0.052)   ~0
21:00 −13:30: mean diff = −0.038 (se 0.064)   ~0
22:00 −13:30: mean diff = −0.081 (se 0.064)   ~0
```

Moving the pin later does **not** improve the realized edge — the paired
differences are within noise, and the point estimates lean negative.

## The honest verdict (the data does NOT support moving the pin late)

The expected outcome was "pin moves late (19:00–22:00) because the low is formed
and the market is still cheap." **The realized numbers say otherwise:**

- **Certainty does rise** as predicted (0.845 → 0.869 by 22:00) — Dan's target
  model is correct about *why* the low market is quiet late.
- **But the realized edge is negative at every pin** (−0.039 to −0.122/contract),
  and **no in-bounds pin is positive** (`any_inbounds_pin_positive: false`).
- **The spread does not collapse** enough to pay for the entry: the market already
  prices the formed low, so late pins buy a *decided* contract at a ~4–6¢ spread
  with no remaining edge — the "cheap because the low is formed" premise is false
  on this cohort.
- **The latest in-bounds pin is 22:00**, but 22:00 is among the *worst*
  (realized −0.122; brier-uncertain 0.219, the forecast gets *worse* late because
  the few remaining uncertain days are the hard ones).
- **The Brier on uncertain days is minimized early** (17:00, 0.1398) — the
  forecast's small remaining edge lives where the low has *not* formed, which is
  *more* likely to be a mid/late-afternoon day than a 22:00 one.

**Recommendation: KEEP the 13:30 incumbent** (or at most 17:00, within noise) and
**do NOT move the pin late.** The late window is not "formed low + still cheap";
it is "formed low + already priced + thinning quotes."

### What would change the answer

This cohort is **9 snapshot days, 84 station-days, 48–60 usable decisions** — the
paired SEs (~0.04–0.06) are larger than every between-pin difference, so the pin
choice is *statistically unresolved* on this data. The honest statement is:

- The 13:30–17:00 window is **at least as good as** anything later, and the
  incumbent has more quote depth.
- Deciding between 13:30 and 17:00 needs more settled station-days (the low
  formed-mid-afternoon cases specifically), not a finer sweep.

## Guardrails

- **Read-only** on every DB. No book writes. No network.
- **Do NOT arm `LOW_BRIDGE_ENABLED`** — blocked on the AG-2 fixes F1–F5
  regardless of pin, and this sweep finds no positive edge to chase.
- The om_gfs model probability is **not trustworthy on this cohort**
  (Brier 0.436 vs market 0.223); any signal that leans on it will lose.
