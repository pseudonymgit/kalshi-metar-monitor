# Bullshit Crawl — Sprint-1 Gate Metric (2026-10-13)

**Item:** run `scripts/bullshit_crawl.py` on the current book (gate: >50 closed trades).
**Book:** `data/paper_trading_sbox.db` — **86 trades / 79 closed / 7 open** (read-only).
**Command:** `python3 scripts/bullshit_crawl.py`
**Artifact:** `data/bullshit_crawl.json`

## Result

| metric | value |
|---|---|
| total trades | 86 |
| flagged | **85** |
| clean | 1 |
| **flagged fraction** | **98.8 %** |
| prior (old saturated book) | 98.8 % |

**Verdict: the calibration regime did NOT bring the flagged fraction down.** It is
identical to the prior saturated-book number.

## Flags per rule

| rule | count |
|---|---|
| extreme edge `(p_fused - market_price)/market_price > 0.50` | 81 |
| S grade on flat signal `p_fused >= 0.9` | 52 |
| low conf but entered `p_fused < 0.6` | 9 |
| bankroll/position mismatch `realized_pnl > $2,000` | 0 |
| over max position `position_size_usd > $25` | 0 |

## Reading

The crawl is dominated by **R5 extreme edge (81)** and **R4 S-grade-on-flat (52)**.
Both are *probability-shape* flags: `p_fused` sits far from `market_price` and often
at/above 0.9. This is the same signal-quality signature as the old book — the
calibration sprint changed *levels* but the book's edge claim is still structurally
extreme (p_fused is over-dispersed relative to the market). Position/risk rules
(R1/R2) are clean: sizing is within the $200-book limits.

**Booked as the Sprint-1 gate number: 98.8 % flagged (85/86).** Gate is not cleared
by calibration alone; it is a signal-shape (p_fused vs market) finding, not a
sizing finding.
