# Market Benchmark Contract — 2026-10-11 (FROZEN)

**Script:** `scripts/market_benchmark_contract.py`  
**Machine artifact:** `data/market_benchmark_contract.json`  
**Axis:** HIGH / greater, strict crossing (`settle > strike`; `==` is LOSS)

## The frozen contract

| choice | value |
|---|---|
| quote filter | `yes_bid > 0 AND yes_ask < 1` (drop degenerate 0/1 books) |
| price | `mid_price = (yes_bid + yes_ask)/2` |
| date alignment | station-**LOCAL** date of `snapshot_time` == ticker target date |
| label | Kalshi settlement truth: `strike_ladder.result` (fallback `kalshi_temp > strike`) |
| strike | parsed from ticker (`strike_temp` is NULL in the DB) |
| CI | block bootstrap by local settlement date, 10000 reps |

## The frozen bar

- **Brier (all quotes) = 0.0208**  n = 144, base rate = 0.0486, 95% CI [0.009, 0.0366] over 42 local settlement dates.
- ⚠️ **n = 144 < 200 — the CI is NOT conclusive at this n.**

### By decision window (UTC hour)

| window | n | Brier | base rate | 95% CI |
|---|---|---|---|---|
| 06Z | 37 | 0.058 | 0.0811 | [0.0019, 0.1127] |
| 10Z | 52 | 0.0465 | 0.0577 | [0.0031, 0.0946] |
| 12Z | 33 | 0.064 | 0.0909 | [0.0022, 0.1365] |
| 14Z | 30 | 0.0387 | 0.0667 | [0.0021, 0.0966] |
| 16Z | 26 | 0.0812 | 0.1154 | [0.0032, 0.2052] |
| 17Z | 29 | 0.0735 | 0.1379 | [0.008, 0.1894] |
| 18Z | 16 | 0.0869 | 0.1875 | [0.0079, 0.1827] |
| 20Z | 7 | 0.0554 | 0.1429 | [0.0002, 0.1683] |
| 22Z | 0 | — | — | — |

## Reliability (canonical cohort) — the honesty check

| mid bin | n | mean mid | empirical |
|---|---|---|---|
| 0.0-0.1 | 127 | 0.028 | 0.008 |
| 0.1-0.2 | 5 | 0.132 | 0.0 |
| 0.2-0.3 | 3 | 0.253 | 0.0 |
| 0.3-0.4 | 1 | 0.325 | 1.0 |
| 0.4-0.5 | 3 | 0.43 | 0.333 |
| 0.5-0.6 | 1 | 0.505 | 0.0 |
| 0.6-0.7 | 2 | 0.647 | 1.0 |
| 0.7-0.8 | 1 | 0.77 | 1.0 |
| 0.8-0.9 | 1 | 0.84 | 1.0 |

## The documented swing (why the bar was unstable)

Brier across every align × label combination (same quote filter):

| align / label | n | Brier | base rate | mean mid |
|---|---|---|---|---|
| local/ladder | 144 | 0.0208 | 0.0486 | 0.0693 |
| local/ksett | 789 | 0.4825 | 0.4702 | 0.1352 |
| local/truth | 564 | 0.4528 | 0.4699 | 0.1339 |
| utc/ladder | 197 | 0.0218 | 0.0457 | 0.0695 |
| utc/ksett | 958 | 0.4922 | 0.4906 | 0.1162 |
| utc/truth | 763 | 0.4818 | 0.5098 | 0.1102 |

## Verdict on the 0.069 claim

The canonical (local alignment + Kalshi `strike_ladder.result` label) market_mid Brier is **0.0208** on n=144, 95% CI [0.009, 0.0366] over 42 local settlement dates.

- The 0.069 figure is **NOT a Brier**: the frozen Brier is 0.0208, while the **mean market mid is 0.0693**. The "0.069" is the market-implied probability level on this far-OTM-heavy cohort, not its Brier score.
- Because the cohort is dominated by deep-OTM quotes (mean mid ≈ 0.07, base rate ≈ 0.05), a low Brier is nearly free: the constant predictor 0.0 already scores 0.0462. The market's edge over that constant is real but small, and n is below the ~200/window bar.
- The `settlement_truth` (GHCN) label yields base rates near 0.43–0.48 with market mids near 0.1 — internally impossible and the source of the swing to ~0.45. It is retained only as the documented unstable alternative; it is NOT canonical. The `kalshi_settlements.kalshi_temp > strike` fallback matches the ladder on the settled set but re-expands to the full quote universe and is not used for the frozen bar.

## Reproduce

```bash
.venv-gefs/bin/python scripts/market_benchmark_contract.py
```
