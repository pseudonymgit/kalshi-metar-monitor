# WE Status Dashboard — Static Surface + Data Source (Item 2, 2026-10-14)

**Date:** 2026-10-14 · **Agent:** Gilfoyle · **Status:** READY — `data/we-dashboard.json` committed, ready for `dashboard(widget_put)`
**Data source:** `data/we-dashboard.json` (committed; un-ignored in `.gitignore`)
**Regenerator:** `scripts/build_we_dashboard.py` (read-only on the book)

---

## Why static + JSON

The live `session:report` widget renders from structured props. Rather than hand-copy numbers into
a widget (which drift), the **data source of truth is `data/we-dashboard.json`** — a committed,
regenerable read-side artifact. The widget is a thin render over it.

```python
dashboard(action="widget_put",
          name="we-status",
          pluginKind="session:report",
          props={ "blocks": load("data/we-dashboard.json") })
```

**Note:** the widget itself lives on the **workspace** session dashboard's surface (the engine repo
has no Control-UI session of its own). This doc + the JSON are the handoff; the render is one call.

---

## Current surface (as of 2026-10-14, commit `4f5c63c90`)

### Book state
- **Open 9 · Closed 79 · Total 88** (SBOX paper book, `data/paper_trading_sbox.db`).
- Constant 1-contract sizing. 82 archived bad-trade rows (p_fused saturated 0.99) excluded.

### Honest metrics (use these — never the look-ahead numbers)
| Metric | Value |
|---|---|
| rate_1h Brier @ 13:30 pin (honest) | **0.36** |
| rate_1h Brier (published, look-ahead) | *0.092 — do not quote* |
| Best single source | **GFS 0.16** |
| Decision ceiling | **0.82–0.86** |
| om_gfs (bc) Brier | 0.123 |
| rate_1h vs NWP fcerr | 0.16–0.30 |

### Fusion
- **5 members**; om_gfs wired (`USE_OM_GFS_FUSION=1`).
- **Agreement boost ON** (`USE_AGREEMENT_BOOST=1`) — explicit n-of-k concurrence term, ≥3 concur
  adds up to +0.03 to `p_core` pre-modulator.

### Sizing
- Max **$10**; bands `>0.70 → $10`, `>0.50 → $5`, else `$0`. ($25 tier removed 2026-10-13.)

### Workstreams
V done · W done · X done · Y done · **Z live** · AA done · **AB queued-cron** (GEFSv12 backfill).

### Blocked
- ECDS second-key (`e543bdb6`) rotation + diagnostic-doc redaction.
- 3 stale Oct-12 opens pending truth / tie rule.
- LOW parity not wired (parity holdout P2 FAILS, parked).

### Revisit
- Per-station weight tuning (data-blocked) · RTMA (re-checked: `DO_NOT_WIRE`) · GEFSv12 (backfill
  cron live) · **GFS v17 rename watch** (SCN not yet issued) · pin sweep (15:00 candidate).

---

## Regenerate

```bash
.venv-gefs/bin/python scripts/build_we_dashboard.py   # refreshes data/we-dashboard.json
```
Read-only on the book; writes only `data/we-dashboard.json`.
