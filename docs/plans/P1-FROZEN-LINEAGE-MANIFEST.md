# P1 — Frozen Code/Config/Data Lineage Manifest

**Item ID:** P1 (Calibrated 90-day performance → freeze code/config/data lineage)
**Status:** FROZEN (snapshot manifest — 90-day rerun must reference this)
**Date frozen:** 2026-09-16
**Owner:** Ray Donovan (implementation + receipt)
**Canonical commit:** `01d9d3a73ae6e7624167b2e56130a77fe05086ae`
**Worktree:** `recovery-worktrees/we-sprint-2026-09-16` (fresh, clean, detached @ canonical tip)

## Purpose

Freeze the exact code, config, and data lineage that a reproducible calibrated 90-day
walk-forward (P2–P4) must reference. This is the "snapshot manifest" — it pins the commit,
the config surface, the dependency set, and the data sources so the rerun is
leakage-safe and reproducible.

## 1. Code lineage (frozen)

- **Canonical commit:** `01d9d3a73` — "chore: untrack non-data generated artifacts (second pass)", 2026-09-15 06:33:33 UTC.
- **Branch:** `temp-sprint-branch` (the committed tip; `we-recovery-2` @ `15e3e6525` is stale/retired).
- **Worktree for this sprint:** `recovery-worktrees/we-sprint-2026-09-16` (detached @ `01d9d3a73`, clean).
- **Not frozen (flag):** the main checkout `weather-engine-source` has 40 modified source + 126 untracked + 8 deleted on top of `01d9d3a73` — UNCOMMITTED sprint work (fusion, signal_evaluator, entry_engine, pipeline, signal_families, ghcn/rdae/sensor re-impls). These are NOT part of this frozen lineage. The 90-day rerun must run against `01d9d3a73`, not the dirty tree.

## 2. Config lineage (frozen)

| File | Role |
|---|---|
| `config.json` | Top-level config (keys: `kalshi`, `spatial_coherence`) |
| `render.yaml` | Deploy config (web service `kalshi-metar-monitor`, start `python app.py`, `PAPER_TRADING_ENABLED=1`) |
| `core/instance_config.py` | Instance config |
| `core/phase1_config.py` | Phase-1 config |
| `core/signal_config.py` | Signal config |
| `scripts/sweep/config.py` | Sweep config (B2 param sweep) |
| `requirements.txt` | Pinned deps (see §3) |

## 3. Dependency lineage (frozen — requirements.txt)

Flask==3.1.2, gunicorn==23.0.0, requests==2.32.5, APScheduler==3.10.4,
cryptography>=42.0.0, numpy>=1.24, scipy>=1.10.0, scikit-learn>=1.3.0,
xgboost>=2.0.0, pandas>=2.0.0.

## 4. Schema/code-of-truth lineage (frozen)

| File | Role |
|---|---|
| `core/db_schema.py` | DB schema (source of truth) |
| `core/db_connection.py` | Connection handling |
| `core/db_utils.py` | DB utilities |
| `core/signal_families.py` | Signal registry (source of truth — 48 entries per 09-14 reconciliation) |
| `core/alert_schema.py` | Alert schema |
| `SIGNAL_REGISTRY.md` / `docs/weather-engine/SIGNAL_REGISTRY.md` | Signal registry docs |

## 5. Data lineage (frozen — 46 DBs under `data/`)

The 90-day rerun consumes settlement, forecast, and observation truth. Key lineage:

| DB | Size | Role |
|---|---|---|
| `data/settlements.db` | 0 B (empty — see flag) | Settlement truth (currently empty) |
| `data/kalshi_settlements.db` | 3.36 MB | Kalshi settlement truth |
| `data/ghcn_settlements.db` | 67 MB | GHCN proxy settlements (21 stations, 619,428 rows per 09-14) |
| `data/nwp_forecasts.db` | 322 MB | NWP forecast archive (May–June short-lead gap closed 09-14) |
| `data/metar_backfill.db` | 2.47 GB | METAR backfill |
| `data/iem_asos.db` | 663 MB | ASOS observations |
| `data/iem_asos_1min.db` | 828 MB | ASOS 1-min observations |
| `data/era5_archive.db` | 3.34 MB | ERA5 reanalysis |
| `data/gefs_archive.db` | 55 MB | GEFS forecast archive |
| `data/kalshi_market_snapshots.db` | 32.5 MB | z8 market snapshots (fresh through 2026-09-16) |
| `data/paper_trading_*.db` | various | Paper-trading state (dev/sbox/live/prod) |
| `data/signal_cache.db` | 19.5 MB | Signal cache |

Full inventory: 46 `*.db` files under `data/`. This manifest pins the *identity and role*
of each; exact row-level hashes are not captured here (that is the snapshot/backup layer's
job — see §6).

## 6. Snapshot/backup lineage (frozen — pointer only)

- Clean non-git snapshot: `/srv/backups/openclaw-next/weather-engine-clean-20260914/` (5.8G, 14,568 files).
- Weekly snapshot + restore drill cadence: E6 (separate item).
- This manifest does NOT copy data; it references the authoritative snapshot for byte-level reproduction.

## 7. Flags / open reconciliation (not silently resolved)

1. **`data/settlements.db` is 0 bytes** (empty) while `kalshi_settlements.db` and `ghcn_settlements.db` hold actual settlement truth. The 90-day rerun must be explicit about which settlement source it reads — this is S-track provenance territory, not resolved here.
2. **Dirty-tree uncommitted work** (40 modified source) is excluded from this frozen lineage. If the 90-day rerun is expected to include that work, it must be committed + merged first (Dan authority).
3. **`data/metar.db` is 4 KB** (nearly empty) vs `metar_backfill.db` 2.47 GB — the canonical METAR source for the rerun must be pinned explicitly.

## Receipt

- **Frozen from:** live `weather-engine-source` checkout (data inventory) + clean `01d9d3a73` worktree (code/config).
- **No rerun executed.** Lineage-freezing only, per preregistration policy.
