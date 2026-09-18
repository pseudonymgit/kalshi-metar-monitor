# Phase 3 ignored-input artifact manifest — 2026-08-17

**Purpose:** reproducible local package for the clean Phase 3 execution checkout. This manifest is metadata only; it does not add ignored databases, raw GEFS objects, credentials, or generated JSON/log residue to Git.

**Package root:** `/home/node/.openclaw/artifacts/weather-engine-phase3-20260817T042657Z/`

**Package files:**

- `README.md` — local materialization paths and retention boundary.
- `db-input-manifest.json` — database byte sizes, SHA-256 values, schema SHA-256 values, and row counts.
- `schema/metar_backfill.schema.sql`
- `schema/kalshi_settlements.schema.sql`
- `gefs-evidence-tree.sha256` — deterministic sorted SHA-256 tree for the two retained GEFS evidence roots.
- `worktree-status-before.txt` and `worktree-classification-664.json` — exact 664-entry classification input and path-level reconciliation.
- `package-sha256.txt` — package metadata hashes.

## Required ignored inputs

| Input | Absolute path | Size | SHA-256 | Schema SHA-256 |
|---|---|---:|---|---|
| `data/metar_backfill.db` | `/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db` | 2457526272 bytes | `b15682ef7df24302668b3c4b9a32e473e29903a5223fdb8d03e03b238967df17` | `8bbad0b9212f45b4a36c3f07796d6ea7ad98c191ac86873bc2fcf69f0531e1c8` |
| `data/kalshi_settlements.db` | `/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/kalshi_settlements.db` | 2072576 bytes | `feeaec1ff683273c82f86948d5c42df749b7ec293477e41b937be9ee98773c82` | `ef20527fa2c27d6a338ba2f618083365fc660082001a5c50d0d49751176599da` |

SQLite table counts are preserved in `db-input-manifest.json`; the package does not copy the databases. The current observed counts are `metar_backfill.db`: `metar_observations=12,824,187`, `daily_stats=58,143`, `settlement_epochs=98,640`, `six_hour_aggregates=3,174`, `station_climatology=4,774`; and `kalshi_settlements.db`: `kalshi_settlements=9,567`.

## GEFS evidence

- Roots retained locally: `data/raw/gefs_temperature_pilot/2026-08-15-f057-recovery-v1` and `data/raw/gefs_temperature_pilot/2026-08-15-timing-retry-v1`.
- Deterministic tree: 535 files, 3550955018 bytes.
- Tree-manifest SHA-256: `6cf9b94265ad7053cea3518fa14d47c8f6028ebd2ba23a44b165509285beace6`.
- The GEFS receipt remains research-only and must not be promoted to score-ready or execution-ready; current QC includes the documented timing/coverage blockers.

## Exclusions and reproducibility boundary

- No `.env`, `.env.webhooks`, PEM/private key, ignored DB/WAL/SHM, Graphify output, or generated logs/JSON is packaged or committed.
- Reproduce the package metadata from the workspace paths above by hashing the two DBs and regenerating a sorted SHA-256 list over both GEFS roots. Do not substitute data or silently use a different source snapshot.
- The 664-entry worktree classification is preserved as an accounting artifact. It is not an authorization to delete/archive any unresolved or unrelated path.

Generated UTC: `2026-08-17T04:30:50.118137+00:00`.
