# Phase 2 Deep QC Receipt — 2026-08-16

**Scope:** read-only inspection of weather-engine cleanup. No delete, move, archive, reset, restore, commit, scheduler alteration, or Graphify touch was performed. Existing dirty-tree state was preserved.

## Decision summary

- **100 archive candidates:** 9 P3, 6 retired signal modules, 83 legacy core/sizer/signal files, 2 backups. All remain **retain pending explicit authorization**; archive-candidate does not mean safe to delete.
- **P3:** **research-only/dead for current sweep.** Active `core/p3_*` files are compatibility imports/wrappers; archived analog implementation is not in `SignalRegistry`. `data/p3_backtest_results.json`: 530/1128 = **46.99%**, p=0.046054, below the 58% target. WS1 recommends adopting SignalRegistry and archiving P3. Resurrect only as a separately preregistered replication after rebuilding `settlement_epochs` and proving data lineage.
- **Retired aliases:** no blanket 100%-redundancy claim is safe. `persistence_signal.py` is an import-path compatibility alias; retired 82-member copy is superseded by active canonical copy; `simple_trend` only conceptually overlaps `metar_trend`; pressure tendency, settlement arbitrage, and volume momentum have no active canonical registry equivalent.
- **Secrets:** `.env.webhooks`, `config.json`, and `data/kalshi_private_key.pem` are tracked; `.env` is ignored/untracked. All four path names occur in Git history. This is an exposure risk. Values were never printed.
- **Dashboard:** `core/trading_dashboard` is a real active Flask weather-engine trading dashboard registered by `app.py` at `/trading`, not a WhaleWatch orphan.
- **Sweep/diagnostic files:** exact reviewed paths and actions are in the machine manifest. Superseded artifacts remain evidence; authoritative artifacts remain immutable; generic results lacking producer identity are unknown/do-not-touch. No blanket deletion is approved.
- **Deletion candidates:** `.venv`, pytest cache, and bytecode are regenerable only in a separately built clean snapshot after dependency/producer proof. SRRS outputs are mixed raw/evidence/derived and require path-level lineage.

## Archive groups

| Group | Count | Disposition |
|---|---:|---|
| `core/archive/2026-08-11*` legacy core/sizers/signals | 83 | old/superseded mixed-value; retain pending reference graph |
| `core/archive/2026-08-11-p3` | 9 | research-only compatibility/recovery; exclude active sweep |
| `core/signals/retired` | 6 | compatibility/audit; not active fusion except canonical 82-member copy |
| backups (`infra/backups`) | 2 | recovery-only; retain until rollback receipt |

The manifest contains the exact 100 paths from the Phase 1 authoritative JSONL and group lists. No candidate is approved for transaction by this receipt.

## P3 lane versus current architecture

Archived P3 is an analog feature extraction → match engine → trajectory tracing → confidence/output/scheduler/API lane. The current `SignalRegistry` has an explicit canonical active-name list and no P3 analog signal. Active P3 modules are retained because tests/compatibility callers still import them; this does not make the analog lane sweep-relevant.

`docs/plans/WS1-P3-RETEST.md` records: P3 unavailable on the current period because `settlement_epochs` is empty; no P3 run tags in the GEFS backtest DB; the original 32.9% result remains the reliable estimate; and **Option A: adopt SignalRegistry, archive P3**. The P3 engine declares a 0.58 target, while its result artifact reports 46.99%. Preserve the result as negative research evidence.

## Retired-to-active alias map

| Retired path | Active/canonical path | Verdict |
|---|---|---|
| `core/signals/retired/persistence_signal.py` | `core/signals/persistence_signal.py` | import-path alias; compatibility-only, not registered |
| `core/signals/retired/eighty_two_member_ensemble_signal.py` | `core/signals/eighty_two_member_ensemble_signal.py` | superseded copy; active registry includes canonical name |
| `core/signals/retired/pressure_tendency_signal.py` | none | compatibility-only intraday module; no active equivalent |
| `core/signals/retired/simple_trend_signal.py` | `core/signals/metar_trend_signal.py` | conceptual overlap, not identical behavior |
| `core/signals/retired/settlement_arbitrage_signal.py` | none | market microstructure compatibility-only |
| `core/signals/retired/volume_momentum_signal.py` | none | market microstructure compatibility-only |

## Secrets/config tracking

Shape-only inspection found credential-shaped dotenv entries, key-id-shaped JSON config, and PEM private-key-shaped material. `git ls-files` confirms current tracking of `.env.webhooks`, `config.json`, and `data/kalshi_private_key.pem`; `.env` is ignored. Git history path inspection finds all four names. `.gitignore` contains unresolved merge-conflict markers and broad patterns, so it is not a reliable exposure control.

**Remediation:** rotate/revoke affected credentials; remove sensitive blobs via an approved transaction; scrub public history if applicable; replace config with a redacted example; restrict private key to 0600; repair ignore rules; rerun a full-ref secret scan without printing matches.

## Dashboard determination

`app.py` imports `core.trading_dashboard.routes.trading_bp` and registers `/trading`. `routes.py` defines page routes plus P&L, positions, portfolio, alerts, risk, stats, and SSE stream APIs. The Blueprint points to `templates/` and `static/`; templates load `/trading/static/dashboard.js`; read-only DB helpers target `paper_trading.db` and `trade_journal.db`. Active package searches found no WhaleWatch references. WhaleWatch modules are separately archived; retain this dashboard.

## Sweep/diagnostic and SRRS treatment

The machine manifest’s `sweepDiagnosticExactCandidates` is the exact reviewed candidate list. Key classifications: the dated sweep report is explicitly historical/pre-remediation; old directional and WS9 v1 artifacts are superseded evidence; post-remediation directional, WS6, and WS9 v2 artifacts are authoritative/protected; generic `sweep_results*.json` files are unknown until producer/hash lineage is proven; P3 results are failure evidence.

SRRS trees mix raw/backfill inputs, QC receipts, manifests, checkpoints, and derived outputs. Preserve raw + manifests + receipts + checkpoint/WAL sidecars. Only omit individually proven deterministic derivatives from a separately built clean snapshot; do not blanket delete.

## Status and license

`LICENSE` is Apache License 2.0. `.improvement-status` (last entry 2026-07-04) and `.phase2-status` (2026-07-02) are stale ledgers. `PROJECT_STATE.md` is a historical/conflicting runtime snapshot. `docs/CURRENT_STATE.md` is active but explicitly non-authoritative. The dated readiness accounting, WS1 P3 retest, and sweep-readiness review are active reference receipts. The historical sweep report is safe to archive only after preserving its supersession banner/link.

## Deletion candidates and clean snapshot policy

| Candidate | Observed | Producer/regen | Treatment |
|---|---|---|---|
| `.venv` | 836M | pinned dependency/bootstrap recipe must be captured | omit from separate clean snapshot only after rebuild/test proof |
| `.pytest_cache` | 184K | pytest | clean-snapshot omit; regenerable |
| `__pycache__`/`.pyc` | 106 dirs, 1,246 files, 27,722,262 bytes | Python import/compile | clean-snapshot omit; no mutation here |
| SRRS derived outputs | mixed | path-specific scripts and immutable raw/QC lineage | no blanket action; preserve evidence/recovery artifacts |

A clean snapshot separately retains source, lock/config metadata, raw inputs, manifests, receipts, checkpoints, and recovery instructions; it omits only proven regenerables. The current dirty checkout is not the clean snapshot.

## Independent QC results

1. Phase 1 JSONL parse/count: **PASS — 100 entries**.
2. Partition and duplicate check: **PASS — 9 P3 + 6 retired + 83 legacy core + 2 backups = 100; no duplicates**.
3. Tracking/history/ignore checks: **PASS — performed without printing values**.
4. Dashboard route/import/template/static search: **PASS**.
5. Cleanup inventory: **PASS — 836M venv, 184K pytest cache, 106 bytecode directories, 1,246 files**.
6. Git status: **PASS — pre-existing dirty state observed; no reset, cleanup, commit, scheduler, or Graphify operation**.

## Recommendations and accuracy improvement

1. Require explicit path-level authorization before archive/deletion.
2. Build a clean execution snapshot with source/producer/hash lineage.
3. Add registry/alias QC gate requiring canonical target, semantic-equivalence verdict, active registration, and tests for every retired signal; fail on silent reintroduction.
4. Add the sweep-readiness target-day weather-read guard at import time.
5. Keep P3 out of the primary sweep; any revisit requires preregistered replication and data/settlement-epoch recovery gates.

Machine-readable manifest: [`phase2-qc-manifest.json`](./phase2-qc-manifest.json).
