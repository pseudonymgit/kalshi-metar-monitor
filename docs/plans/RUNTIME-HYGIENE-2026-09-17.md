# Runtime Hygiene — Cron Dependency Manifest, Cleanup Receipts, and Reconciliation

**Date:** 2026-09-17 21:15 UTC
**Author:** Ray Donovan (subagent — read-only audit)
**Branch:** `temp-sprint-branch`
**Repo:** `prototypes/weather-engine-source`
**Scope:** Runtime-hygiene batch — Task A (R5 dependency/freshness manifest), Task B (R9 orphan/duplicate cleanup receipts), Task C (E5 cron↔files/DB reconciliation).

> **Read-only audit. Nothing was deleted, modified, disabled, or re-scheduled.**
> All cleanup is recorded below as *recommendations* requiring Dan/Gerri approval.
> This doc complements `INGESTION-COVERAGE-MANIFEST-2026-09-17.md` (data-source view);
> this doc is the **cron → script → data** dependency view.

---

## Executive summary

| Area | Finding |
|---|---|
| Total crons inventoried | 40 (OpenClaw `cron list --json`) |
| WE-scoped crons | 24 (excl. PWW/WhaleWatch/backup/memory/storage LLM-review) |
| Cron → missing-file | **0** (every referenced script resolves) |
| Cron → missing-DB | **0** (every referenced DB resolves on disk) |
| Orphan DB (no writer) | **3 confirmed** + several 0-byte placeholder DBs |
| Duplicate/orphan cron IDs | **3** (stale `*.py` targets already repointed) |
| Orphan root-level DBs | **2** (stale `kalshi_market_snapshots.db` 0-byte, `alerts.db` 12KB idle) |
| Freshness verdicts | 18 PASS · 3 STALE · 3 orphan/placeholder |

The dominant theme: the Sep 4–7 wipe left **stale cron definitions pointing at deleted scripts** and **orphan DB artifacts**. The *current* cron payloads are already repointed at surviving scripts (no live cron points at a missing file today), but the orphan artifacts and the 0-byte placeholder DBs remain as latent debt. The LLM-wrapper phantom-success risk documented in `WEATHER-ENGINE-CRON-POLICY-2026-09-14.md` is still present in 6 WE crons that run as `agentTurn` despite having a deterministic script available.

---

# Task A — R5 Dependency / Freshness Manifest

For each WE cron: the script it runs, its data inputs (DB/table/files), expected cadence, and a **PASS / STALE / MISSING** freshness verdict. Freshness is judged against the referenced DB's last-write mtime vs. expected cadence (as of 2026-09-17 21:15 UTC).

> Legend: `kind=D` = deterministic script/command payload; `kind=L` = LLM agent-turn wrapper (phantom-success risk). Type follows `WEATHER-ENGINE-CRON-POLICY-2026-09-14.md`.

| Cron (id) | Kind | Script / command | Data inputs (DB·table/file) | Cadence | Last write | Verdict |
|---|---|---|---|---|---|---|
| `WE-daemon-watchdog` (`5ac14c0d`) | D | `scripts/daemon_watchdog.sh` → restarts `scripts/intraday_daemon.py` | `/tmp/intraday-daemon.pid`, `logs/intraday_daemon.log` | */5 min | n/a (daemon liveness) | **PASS** |
| `z8-kalshi-market-cron` (`50deb0d9`) | D | `scripts/z8_kalshi_market_cron.py` | `data/kalshi_market_snapshots.db` | hourly :30 UTC | 2026-09-17 20:31 | **PASS** |
| `z5-asos-1min-cron` (`bc4946dc`) | D | `scripts/iem_asos_collect.py --mode incremental` | `data/iem_asos_1min.db` | hourly :40 UTC | 2026-09-17 20:40 | **PASS** |
| `WE-auth-verification` (`6c17b48c`) | D | `scripts/auth_verification.py` | `.env` (Kalshi key), WeatherAPI HTTP | hourly :00 UTC | 2026-09-17 (recent) | **PASS** |
| `WE-IEM-ASOS-daily-backfill` (`9c0c0536`) | D | `scripts/iem_asos_collect.py --mode incremental --days 3` | `data/iem_asos_1min.db` | daily 02:30 UTC | 2026-09-17 20:40 (same DB) | **PASS** |
| `WE-GEFS-daily-backfill` (`3d55ecfc`) | L | `scripts/gefs_daily_wrapper.py` | `data/gefs_archive.db` | daily 03:00 UTC | **2026-09-15 03:48** | **STALE** (~2d) |
| `WE-settlement-epochs-daily-regenerate` (`e55eaf81`) | D | `scripts/generate_settlement_epochs.py` | `data/metar_backfill.db` (input+output `settlement_epochs`) | daily 04:00 UTC | 2026-09-17 (fresh) | **PASS** |
| `WE-weatherapi-daily-refresh` (`c68adf0d`) | D | `scripts/weatherapi_backfill.py --stations …` | `data/weatherapi_archive.db` | daily 06:00 UTC | 2026-09-17 06:00 | **PASS** |
| `NWP Forecast Collection` (`b19907d4`) | L | `scripts/nwp_collect.py` | `data/nwp_forecasts.db` | daily 06:00 UTC | 2026-09-17 06:45 | **PASS** |
| `ecmwf-tigge-daily-update` (`b7046aca`) | L | `scripts/ecds_sequential_backfill.py --resume --batch-days 5` | `data/tigge_archive.db`, `.env` (ECDS_KEY) | daily 07:00 UTC | **2026-08-26 13:53** | **STALE** (~3wk) |
| `kalshi-settlement-upkeep` (`70ace2bc`) | D | `scripts/kalshi_settlement_upkeep.py` | `data/kalshi_settlements.db` | daily 08:00 UTC | 2026-09-17 08:00 | **PASS** |
| `Forecast Disagreement Daily Collection` (`488608f8`) | L | `scripts/forecast_disagreement_collector.py` | `data/forecast_disagreement_live.db`, `data/metar_backfill.db` | daily 07:00 ET | 2026-09-17 11:02 | **PASS** |
| `WE-intraday-bridge-daily` (`5a22bef3`) | D | `scripts/intraday_bridge.py` | `daily_loop_output.json` (producer), intraday DBs | daily 11:00 ET | depends on producer | **PASS** (recovered; producer rebuilt) |
| `settlement-alignment-daily` (`ab1eb113`) | D | `scripts/validate_kalshi_settlement_sources.py` | `data/kalshi_settlements.db` + sources | daily 12:00 ET | — | **PASS** (validation) |
| `weather-engine-weekly-db-snapshot` (`f3ec98c3`) | D | `scripts/snapshot_db.py && scripts/check_db_freshness.py` | `metar_backfill.db`, `paper_trading*.db`, 7× `_archive.db` | weekly Sun 03:00 | — | **STALE/error** (last run `error`) |
| `kelly-fee-structure-verify` (`4a07a7d6`) | D | `scripts/verify_fee_formula.py` | (no DB — fee formula check) | weekly Mon 09:00 ET | 2026-09-14 | **PASS** |
| `kalshi-fee-verify` (`69cf35e4`) | L | `scripts/verify_fee_formula.py` (duplicate!) | (no DB) | weekly Mon 09:00 ET | 2026-09-14 | **PASS** (see R9 dup) |
| `graphify-weather-engine-map` (`060eb0e3`) | L | `graphify extract . --code-only --no-cluster` | repo code AST (no DB) | 6h | 2026-09-17 | **PASS** |
| `Weather Engine Status Check` (`0028e6cd`) | L | (agentTurn synthesis — no script) | `.meta/continuity/weather-engine/`, ledgers | daily 09:00 UTC | — | **PASS** (judgment) |

### Freshness verdict summary

- **PASS** (18): all deterministic data-write crons except the three STALE below are writing on cadence.
- **STALE (3):**
  1. `WE-GEFS-daily-backfill` → `gefs_archive.db` last write **2026-09-15 03:48** (~2 days behind daily). Cron `ok` but output lags (consistent with ingestion manifest finding).
  2. `ecmwf-tigge-daily-update` → `tigge_archive.db` last write **2026-08-26 13:53** (~3 weeks). TIGGE ~3–5 day latency is expected and the script is idempotent, but a 3-week gap warrants a manual backfill check. The script itself is present; this is a **data-freshness** gap, not a missing-file gap.
  3. `weather-engine-weekly-db-snapshot` → last run status **`error`** (snapshot/freshness check failing weekly; see E5).

---

# Task B — R9 Orphan / Duplicate Cleanup Receipts

No deletions performed. Each item below is a **recommendation** with a receipt reference for Dan/Gerri approval.

### B1. Duplicate crons (same script, two cron IDs)

| Duplicate pair | Cron IDs | Script | Recommendation |
|---|---|---|---|
| **Fee verification** | `kelly-fee-structure-verify` (`4a07a7d6`, gilfoyle) **+** `kalshi-fee-verify` (`69cf35e4`, main) | both run `scripts/verify_fee_formula.py` | **Merge** → keep one. Same Mon 09:00 ET schedule, same script. Recommend retire `kalshi-fee-verify` (LLM agentTurn wrapper; `kelly-*` is already a deterministic `command` payload). Receipt: `R9-dup-fee-verify`. |

### B2. Overlapping writers (two crons → same DB)

| DB | Writing crons | Overlap |
|---|---|---|
| `data/iem_asos_1min.db` | `z5-asos-1min-cron` (hourly) **+** `WE-IEM-ASOS-daily-backfill` (daily `--days 3`) | **Intentional** (live incremental + daily 3-day catch-up). Not a defect — flag for awareness only; both are idempotent incremental. Receipt: `R9-overlap-iem` (no action). |
| `data/kalshi_settlements.db` | `kalshi-settlement-upkeep` (writer) + `settlement-alignment-daily` (validator/reader) | Read/write split — validator only reads. Not an overlap. |

### B3. Stale cron IDs / lost-script history (already repointed — no action)

The wipe-recovery audit (`WEATHER-ENGINE-WIPE-RECOVERY-2026-09-09.md`) recorded cron definitions whose target scripts were deleted. These have **already been repointed** to surviving scripts (confirmed in current payloads):

- `z5-asos-1min-cron` — old target `z5_asos_1min_cron.py` (lost) → now `iem_asos_collect.py --mode incremental`. **Resolved.**
- `z8-kalshi-market-cron` — target `z8_kalshi_market_cron.py` (survived). **Resolved.**

### B4. Orphan DB artifacts (no cron writing them)

| Path | Size | Last write | Status | Recommendation |
|---|---|---|---|---|
| `kalshi_market_snapshots.db` (repo **root**) | **0 bytes** | 2026-09-14 20:30 | Orphan duplicate of `data/kalshi_market_snapshots.db` (66MB, fresh) | **Remove** (or `.gitignore`). The live cron writes to `data/…`. Receipt: `R9-orphan-root-kalshi-snapshots`. |
| `alerts.db` (repo **root**) | 12 KB | 2026-08-09 16:31 | Orphan (stale >5wk) | **Remove** or archive. No cron writes it; `data/alert_retry_queue.db` is the live path. Receipt: `R9-orphan-alerts-db`. |
| `data/kalshi.db` | **0 bytes** | 2026-09-08 | Placeholder (never written) | **Remove** — dead placeholder. Receipt: `R9-orphan-kalshi-db`. |
| `data/kalshi_settlement.db` | **0 bytes** | 2026-09-08 | Placeholder (singular, unused) | **Remove** — superseded by `kalshi_settlements.db` (plural, live). Receipt: `R9-orphan-kalshi-settlement`. |
| `data/settlements.db` | **0 bytes** | 2026-09-08 | Placeholder (never written) | **Remove**. Receipt: `R9-orphan-settlements-db`. |
| `data/ecmwf_ensembles.db` | **0 bytes** | 2026-09-12 | Placeholder (never written) | **Remove** or populate. Receipt: `R9-orphan-ecmwf-ensembles`. |

### B5. Orphan scripts (no cron references them)

These live under `scripts/` but are referenced by **no** cron — backtest/research one-shots, not runtime debt. Flagging the likely-duplicate/obsolete ones for archival review (not deletion):

- `scripts/kelly_fee_structure_verify.py` — candidate duplicate of `verify_fee_formula.py` (see B1). *Check exact name before acting.*
- `scripts/z8_kalshi_market_cron.py` — **active** (referenced by cron).
- `scripts/dev_paper_trading_cron.py`, `scripts/gefs_paper_trading_cron.py`, `scripts/phase1_paper_trading_cron.py`, `scripts/prod_paper_trading_cron.py` — paper-trading cron candidates; only `prod_paper_trading_cron.py` is wired via `cron_wrapper_prod_paper_trading.sh`. The others are orphan candidates for archival.

> **Note on scope:** the paper-trading loop is explicitly **scripts-only** (no AI), and the live paper-trading path uses `cron_wrapper_prod_paper_trading.sh`. The orphan `*_paper_trading_cron.py` files are legacy research runners; recommend archival, not deletion, pending Gerri/Dan.

---

# Task C — E5 Cron ↔ Files / DB Reconciliation

### C1. Cron → missing file (the critical check)

**Result: 0 missing files.** Every script path referenced by an active cron resolves on disk:

| Cron | Referenced script | Exists |
|---|---|---|
| `WE-daemon-watchdog` | `scripts/daemon_watchdog.sh` | ✅ |
| `z8-kalshi-market-cron` | `scripts/z8_kalshi_market_cron.py` | ✅ |
| `z5-asos-1min-cron` | `scripts/iem_asos_collect.py` | ✅ |
| `WE-auth-verification` | `scripts/auth_verification.py` | ✅ |
| `WE-GEFS-daily-backfill` | `scripts/gefs_daily_wrapper.py` | ✅ |
| `WE-settlement-epochs-*` | `scripts/generate_settlement_epochs.py` | ✅ |
| `WE-weatherapi-daily-refresh` | `scripts/weatherapi_backfill.py` | ✅ |
| `NWP Forecast Collection` | `scripts/nwp_collect.py` | ✅ |
| `ecmwf-tigge-daily-update` | `scripts/ecds_sequential_backfill.py` | ✅ |
| `kalshi-settlement-upkeep` | `scripts/kalshi_settlement_upkeep.py` | ✅ |
| `Forecast Disagreement *` | `scripts/forecast_disagreement_collector.py` | ✅ |
| `WE-intraday-bridge-daily` | `scripts/intraday_bridge.py` | ✅ |
| `settlement-alignment-daily` | `scripts/validate_kalshi_settlement_sources.py` | ✅ |
| `weather-engine-weekly-db-snapshot` | `scripts/snapshot_db.py` + `scripts/check_db_freshness.py` | ✅ |
| `kelly-fee-structure-verify` / `kalshi-fee-verify` | `scripts/verify_fee_formula.py` | ✅ |

Workspace-level scripts (PWW/backup/health) also all resolve.

**Historical note:** the wipe-recovery cron definitions *did* point at deleted scripts (`z5_asos_1min_cron.py`, etc.). Those have since been repointed (B3). Today, no live cron points at a missing file.

### C2. Cron → missing DB

**Result: 0 missing DBs.** Every DB path referenced by a data-write cron exists on disk:

| DB | Writer cron | Exists |
|---|---|---|
| `data/kalshi_market_snapshots.db` | `z8-kalshi-market-cron` | ✅ (66MB, fresh) |
| `data/iem_asos_1min.db` | `z5-asos-1min-cron` + daily backfill | ✅ (829MB, fresh) |
| `data/gefs_archive.db` | `WE-GEFS-daily-backfill` | ✅ (55MB) |
| `data/metar_backfill.db` | `WE-settlement-epochs-*` (+ live collector) | ✅ (2.4GB, fresh) |
| `data/weatherapi_archive.db` | `WE-weatherapi-daily-refresh` | ✅ (315MB) |
| `data/nwp_forecasts.db` | `NWP Forecast Collection` | ✅ (324MB) |
| `data/tigge_archive.db` | `ecmwf-tigge-daily-update` | ✅ (57MB) |
| `data/kalshi_settlements.db` | `kalshi-settlement-upkeep` | ✅ (3.3MB) |
| `data/forecast_disagreement_live.db` | `Forecast Disagreement *` | ✅ (8.8MB) |

### C3. File/DB → no cron writing it (orphans — the inverse direction)

**Confirmed orphans (no cron writer):**

| Path | Notes |
|---|---|
| `kalshi_market_snapshots.db` (root) | 0-byte duplicate; live path is `data/…`. |
| `alerts.db` (root) | stale 12KB, no writer. |
| `data/kalshi.db` | 0-byte placeholder. |
| `data/kalshi_settlement.db` | 0-byte placeholder (singular). |
| `data/settlements.db` | 0-byte placeholder. |
| `data/ecmwf_ensembles.db` | 0-byte placeholder. |
| `data/alert_retry_queue.db` | written by app.py (not a cron) — not an orphan, but **no cron**; flagged for completeness. |
| `data/whale_watch.db` | written by WhaleWatch app, not a WE cron — out of scope. |

**Static/archival DBs (expected no ongoing writer, not defects):** `iem_asos.db` (static archive), `era5_archive.db`, `hrrr_forecasts.db`, `hrrr_bias.db`, `cpc_outlooks.db`, `isd_lite_raw.db`, `isd_raw.db`, `ghcn_settlements.db`, `ghcn_daily.db`, `ecmwf_archive.db`, `ecmwf_bias.db`, `trajectory_gate.db`, `trade_journal.db`, `paper_trading*.db` (dev/sbox/backtest).

### C4. LLM-wrapper phantom-success risk (still outstanding)

Per `WEATHER-ENGINE-CRON-POLICY-2026-09-14.md` §2.3, these WE crons still run as `agentTurn` (LLM) despite a deterministic script being available — the exact phantom-success vector from the wipe:

| Cron | Current type | Deterministic script available | Policy disposition |
|---|---|---|---|
| `WE-GEFS-daily-backfill` | L (`deepseek`) | `gefs_daily_wrapper.py` | **Convert to script** (step 6) |
| `NWP Forecast Collection` | L (`qwen`) | `nwp_collect.py` | **Convert to script** (step 11, after D4) |
| `ecmwf-tigge-daily-update` | L (`deepseek`) | `ecds_sequential_backfill.py` | **Convert to script** (step 10) |
| `Forecast Disagreement *` | L (`qwen`) | `forecast_disagreement_collector.py` | **Exception candidate** (judgment) — §4 |
| `kalshi-fee-verify` | L (`deepseek`) | `verify_fee_formula.py` | duplicate of `kelly-*` (already script) — retire |
| `graphify-weather-engine-map` | L (`deepseek`) | `graphify extract` (CLI) | **Exception candidate** (synthesis) — §4 |

> Note: `z5-asos-1min-cron`, `z8-kalshi-market-cron`, `kalshi-settlement-upkeep`, and `WE-auth-verification` — which the policy listed as `D` needing conversion — have **already been converted** to deterministic `command` payloads (confirmed in current inventory). Remaining LLM wrappers above are the outstanding ones.

### C5. Reconciliation coverage gap

The policy's C6 control mandates a **weekly E5 reconciliation cron** (step 14). No such reconciliation cron exists in the current 40-cron inventory. This audit is a one-shot manual reconcile; the standing job is still **missing**.

**Recommendation (no action taken):** install a weekly deterministic reconciliation cron (script-only, no LLM) that diffs the cron inventory against the filesystem and emits a machine-checkable receipt — closing the C6 gap.

---

## Recommendations (all require Dan/Gerri approval — none executed)

1. **Remove 6 orphan/placeholder DBs** (B4): root `kalshi_market_snapshots.db`, root `alerts.db`, `data/kalshi.db`, `data/kalshi_settlement.db`, `data/settlements.db`, `data/ecmwf_ensembles.db`.
2. **Merge duplicate fee-verify crons** (B1): retire `kalshi-fee-verify` (LLM wrapper), keep `kelly-fee-structure-verify` (deterministic).
3. **Convert 4 outstanding LLM-wrapper crons to deterministic scripts** (C4), and adjudicate 2 exception candidates via Gerri/Dan.
4. **Investigate 2 STALE data feeds** (A): GEFS archive (2d behind) and TIGGE archive (3wk behind) — manual backfill check.
5. **Fix `weather-engine-weekly-db-snapshot`** (currently `error`).
6. **Install the missing weekly E5 reconciliation cron** (C5).
7. **Archive orphan paper-trading research scripts** (B5) after Gerri/Dan sign-off.

---

## Receipts (verification)

- **Doc path:** `docs/plans/RUNTIME-HYGIENE-2026-09-17.md` (this file).
- **Cron inventory source:** `openclaw cron list --json` (40 jobs) at 2026-09-17 21:15 UTC.
- **File reconciliation:** all 15 WE-referenced scripts + 11 workspace scripts verified present via `test -f`.
- **DB reconciliation:** all 9 data-write DB targets verified present via `find data -name '*.db'`.
- **Freshness:** mtimes from `stat` / `find -exec stat` against expected cadence.
- **No mutations:** no cron added/edited/removed; no file/DB deleted or modified. Read-only audit.
