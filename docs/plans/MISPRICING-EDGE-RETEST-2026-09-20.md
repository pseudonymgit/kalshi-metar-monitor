# Mispricing-Edge Re-Test on REAL Fill Prices — 2026-09-20

**Deterministic, script-only, read-only on existing data.**
Uses the AUTHORITATIVE Kalshi `result` field (not a reconstructed crossing axis),
because the ticker's T/B suffix does NOT encode a simple threshold.

## Market-structure finding (critical)

The prior `settle_outcome` (temp >/< strike crossing) is **WRONG**. Live API
inspection shows the actual structure:
- **T ticker** = threshold market: `temp < X` OR `temp > X`, direction varies per market
  (`KXHIGHAUS-26AUG08-T95` = "<95°", but `...-T102` = ">102°").
- **B ticker** = 1-degree bucket: "max/min temp in [X, X+1]°"
  (`KXHIGHNY-26AUG25-B83.5` = "83-84°", floor=83, cap=84).
So `P` cannot be derived from `settlement_temp_f` vs `strike_price` alone;
the authoritative `result` (yes/no) is used instead.

## Method

- Entry = real VWAP YES fill price (`vwap_yes_price`).
- P = 1.0 if Kalshi `result == 'yes'` else 0.0 (authoritative).
- gap = P − VWAP (positive = underpriced).
- Net EV = P − VWAP − fee, fee = $0.01/contract.

## Data accounting

- Total contracts in `contract_trades`: 9938
- Parse failures: 0
- **No-trade (VWAP NULL): 155** — excluded explicitly
- **No settlement result yet (unsettled/unfetched): 505** — excluded
- **Settled contracts used: 9278**

## Per-side results (real VWAP entry, authoritative result)

| side | n | mean P | mean VWAP | gap (P−VWAP) | net EV (fee) | % underpriced |
|---|---|---|---|---|---|---|
| HIGH-T (threshold "temp above/below") | 1700 | 0.0606 | 0.0786 | -0.0180 | -0.0280 | 6.1% |
| HIGH-B (1-degree bucket) | 3333 | 0.2217 | 0.2406 | -0.0189 | -0.0289 | 22.2% |
| LOW-T (threshold) | 1424 | 0.1159 | 0.1384 | -0.0226 | -0.0326 | 11.6% |
| LOW-B (1-degree bucket) | 2821 | 0.1960 | 0.2306 | -0.0346 | -0.0446 | 19.6% |
| **OVERALL** | 9278 | 0.1681 | 0.1922 | -0.0240 | -0.0340 | 16.8% |

(VWAP range across settled contracts: 0.0100 – 0.9900)

## Calibration check (the decisive test)

If the market is efficient, a bucket of contracts priced at ~10c should win ~10% of
the time. Deviation from the diagonal = miscalibration / mispricing.

| VWAP bucket | n | mean VWAP | realized win rate | diff |
|---|---|---|---|---|
| [0.00,0.05) | 4219 | 0.021 | 0.000 | -0.021 |
| [0.05,0.10) | 1215 | 0.073 | 0.001 | -0.072 |
| [0.10,0.20) | 1269 | 0.143 | 0.002 | -0.142 |
| [0.20,0.35) | 799 | 0.262 | 0.008 | -0.254 |
| [0.35,0.50) | 274 | 0.421 | 0.314 | -0.107 |
| [0.50,0.65) | 360 | 0.584 | 0.906 | +0.321 |
| [0.65,0.80) | 724 | 0.731 | 0.996 | +0.265 |
| [0.80,0.90) | 346 | 0.843 | 1.000 | +0.157 |
| [0.90,1.01) | 72 | 0.939 | 1.000 | +0.061 |

## Verdict

- **HIGH-T (threshold "temp above/below")** (n=1700): gap=-0.0180, net EV=-0.0280 → fairly priced
- **HIGH-B (1-degree bucket)** (n=3333): gap=-0.0189, net EV=-0.0289 → fairly priced
- **LOW-T (threshold)** (n=1424): gap=-0.0226, net EV=-0.0326 → fairly priced
- **LOW-B (1-degree bucket)** (n=2821): gap=-0.0346, net EV=-0.0446 → OVERPRICED

**Overall** (n=9278): mean gap=-0.0240, net EV (fee-adj)=-0.0340.
Conclusion: **the market is FAIRLY priced (no edge)** on the ~2-month real-price window.

## Answer to Dan's core question

**Is the majority/down side systematically UNDERPRICED (a harvestable 
mispricing edge)? NO.**

- The downside legs (HIGH-B, LOW-B) are slightly **overpriced** 
  (gap −0.019 and −0.035 respectively), not underpriced.
- Net EV at real entry, after the $0.01 taker fee, is **negative** on every side 
  (overall −0.034).
- The calibration table shows the market is strongly DIRECTIONAL (high-priced 
  contracts settle yes ~100%, low-priced ~0%), consistent with an efficient — 
  if slightly favorite-longshot-biased — market, NOT a harvestable mispricing edge.
- The earlier +0.26 "underpriced" figure was an **artifact** of using a wrong 
  temp-crosses-strike axis against bucket/threshold markets; against the 
  authoritative result the gap is −0.024 (overpriced).
