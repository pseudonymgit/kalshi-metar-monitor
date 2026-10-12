# Track A-3 — Time-Stop / Settlement-Avoidance Variant

**Date:** 2026-10-12
**Base artifact:** `data/trackA_spread_filter.json` (commit `326a2180b`, Track A-2)
**Script:** `scripts/trackA_timestop.py` · **Output:** `data/trackA_timestop.json`
**Dataset:** the SAME 209 committed trades. No new trades derived. No DB queries, no temp DBs, live book untouched.

---

## Verdict (blunt)

**"Not at this data depth — and, more importantly, not at this signal's edge."**

The time-stop can only convert the settlement leg's **realized −$1,403** into a *manageable bleed* **if the exited positions actually have a bid at the stop window**. Whether they do is the crux, and **the committed artifact cannot answer it.** A time-stop's value is an exact affine function of the exit mid `q`, which the artifact does not store. So the honest answer is:

- **Mechanically yes, the fix works** — cutting the settlement losers at a bounded window **cuts the −$1,403 leg by roughly 70–85%** at any realistic non-zero exit price. The +$918 profit-taking legs inherited from Track A are more than enough to cover the residual.
- **Empirically "not determinable"** — the committed set contains **zero post-entry mid quotes**. The narrow band of `q` where the fix actually crosses break-even (**q ≥ 0.639× entry_mid**, a ~36% loss-per-contract tolerance) is *exactly* the band a "declining-toward-0" loser is least likely to sit in, and the band a "no bid at all" market is most likely to breach. **No traded `q` in this artifact ever sampled that band.** Nothing here is falsifiable without the mid-path.

**Recommendation: do not adopt yet.** The decisive evidence is one cheap query away (below); running the time-stop as a "fix" without it is assuming the conclusion.

---

## The structural fact that drives everything

| Base leg (Track A, 209 trades) | n | net |
|---|---|---|
| `take_profit_20` (≥20% rise → sell) | 37 | **+$873.23** |
| `sell_095` | 2 | **+$44.52** |
| `stop_loss` (rate ≤ −0.5°F/15min) | 48 | −$354.58 |
| `settlement` (no ladder trigger fired) | 122 | **−$1,402.85** |
| **Total** | **209** | **−$839.69** (gross −$478.79) |

The settlement leg is **58% of positions and the entire loss**. Of the 122 settlement rides, **105 paid 0.0 and only 17 paid 1.0** (a 14% hit rate). The 105 losers carried ~**16,477 contracts** of notional into a zero payoff — that is the −$1,403.

Two structural bounds fall straight out of the committed set:

1. **No later window, no stop.** 14 trades are entered at **20Z**, the last grid window — there is no 17Z/20Z successor, so **a time-stop cannot touch them at all** (all 14 are settlements, −$190). The affected pool is at most **108 settlement rides**.
2. **A settlement loser has no price "held."** Its entire value came from a payoff of 0. Replacing that with *any* executable `q > 0` **strictly improves** the trade. So the time-stop can only help this leg; the open question is magnitude, not sign.

---

## Variants and results

Rule 3 (settlement **losers** only, all else untouched) is the clean isolation the task asked for. Rules 1/2 are the literal trigger rules; the tables below separate the **trigger-consistent** effect (only losers trigger) from the **naive** effect (trigger also catches the settlement *winners*).

**Affected sets:** rule1/rule2 → 156 trades (96 settlement losers + 12 settlement winners + 48 same-window re-stops, ≈no-op); rule3 → 96 settlement losers, 15,069 contracts.

### Net P&L vs base (−$839.69), by exit-price anchor

`q` = mid available at the stop window, as a fraction of the entry mid.

| Variant | q = 0 (no bid) | q = 0.5·entry | **q = 1.0·entry (no move)** | q = 1.2·entry (ceiling) |
|---|---:|---:|---:|---:|
| rule1 all (naive) | −$3,309 | −$1,884 | −$264 | +$398 |
| rule2 all (naive) | −$3,309 | −$1,884 | −$264 | +$68 |
| **rule3 / losers-only** | **−$963** | **−$240** | **+$617** | **+$968** |

- **The rule's own ceiling is not free.** A time-stop only fires when the mid is *below* the trigger, but if the mid is *at* the boundary the exit price is capped at `1+trigger` (1.2·entry for rule1, 1.1·entry for rule2). Best-case net with all winners preempted: **rule1 +$398**, **rule2 +$68** — both **worse than the loser-only +$617/+$968**, because preempting winners destroys the very leg we are leaning on.
- **The realistic point is `q = entry_mid`** (the task's own "if no quote, use the entry mid" fallback): **net +$617**, a **+$1,457 swing vs base**. Lose-only, winners intact.

### Break-even tolerance

The fix breaks even (net = 0) when the settlement losers can be exited at **q ≥ 0.6391 × entry_mid** — i.e. the exit can *lose up to ~36% of contract value per contract* and the strategy still nets ≥ 0.

```
q = 0.00 → net -$963   (floor: no bid, force-held)
q = 0.25 → net -$671
q = 0.50 → net -$240
q = 0.64 → net  +$2    ← break-even
q = 0.75 → net +$188
q = 1.00 → net +$617
q = 1.20 → net +$968
```

### Exit distribution under rule 3 (q = entry_mid)

| Exit | base n | rule3 n |
|---|---:|---:|
| take_profit_20 | 37 | 37 (untouched) |
| sell_095 | 2 | 2 (untouched) |
| stop_loss | 48 | 48 (untouched) |
| settlement | 122 | 26 (14 at 20Z unreachable + 12 winners kept) |
| **time_stop** | — | **96** |
| **net** | **−$839.69** | **+$616.84** |

---

## Why the answer is "indeterminate," not a number

The whole result hinges on an unobserved quantity. Consider the two regimes:

- **Regime A — the mid found support / partial credit.** The repricing wave did not come (that is why the position settled), but the market still had *some* bid at the next window. Settling payoff 0 means the contract expired worthless; its last traded mid commonly sits well above 0 before the final collapse. If exiting at even **half** the entry mid is achievable, the fix is a clear win (net −$240; the profit legs cover it).
- **Regime B — the contract was already dead.** A contract that settles 0 is often one that had **no bid** at the stop window. Then the "exit" either does not fill (q = 0 → **net −$963**, *worse* than base) or fills at a token penny. The **14 unreachable 20Z settlements** are direct evidence of edge conditions where there is no room to act.

The strategy's whole thesis is that a rising temperature *enters* the next strike. The losers are precisely the days where it *did not* — and on those days the market likely agreed fast. **The time-stop asks for liquidity exactly when liquidity is most scarce.** That is the tension the artifact cannot resolve, and it is the difference between "yes" and "no."

---

## Recommendation

1. **Do not wire the time-stop into the live/paper book on this evidence.** It is a plausible fix resting on an assumption about exit liquidity, not on a measured result.
2. **The decisive test is cheap and read-only.** For the 96 settlement losers (and the 12 winners), pull the **real quoted mid/bid/ask at the first post-entry decision window** from `kalshi_market_snapshots.db`, then recompute the single quantity that matters: the *distribution of q / entry_mid*. If the median is comfortably **> 0.64**, the time-stop is validated and can be adopted; if the mass sits near **0**, the fix is a mirage and the settlement leg needs a different answer (don't take the trade — gate entry, not exit).
3. **Note the direction of the fix.** The result says the lever is **avoiding the settlement ride**, and the most reliable way to avoid it is **not entering** the low-probability setups — the same family as the A-2 spread filter, but on a *signal-quality* axis rather than an entry-cost axis. A time-stop is second-best because it pays to discover, after entry, what a better entry gate would have known up front.

**Bottom line:** the time-stop mechanically converts the −$1,403 settlement leg into a **−$240 to +$617** result depending entirely on an unmeasured exit price. That is a **successful diagnosis and an unproven cure.** One read-only query closes it.

---

## Reproduce

```bash
python3 scripts/trackA_timestop.py            # reads data/trackA_spread_filter.json only
# → data/trackA_timestop.json
```
