# P1 — Lineage Freeze (2026-09-17)

Workstream P, item P1. Establishes a frozen record of which **code version**, **config**, and
**data assets** define the reproducible baseline for results produced from this point forward.

> **Scope:** read-only inspection. No code/config/data was mutated during this freeze.
> Untracked files present at capture are noted but **not** modified.

---

## 1. Frozen Code Baseline

| Field | Value |
|---|---|
| Branch | `temp-sprint-branch` |
| Commit (full) | `04be98a0446f4a22a98cd268b1129376de6442ba` |
| Commit (short) | `04be98a04` |
| Tree hash (`HEAD:`) | `6cbf22904d338bf5f5340b32a6688487612f846c` |
| Author | Dan |
| Commit date (UTC) | 2026-09-16 16:13:05 |
| Subject | `chore(weather-engine): reconcile August main-lineage work into recovery lineage` |

Recent lineage (10 commits, oldest→newest):
```
124fd17ed Add host-level weather daemon supervisor installer
15e3e6525 Fix supervisor installer heredoc escaping
ccf4c9739 Recover 7 missing WE operational source files
75d522b5a chore: fix .gitignore merge conflict, untrack data/ and generated artifacts
01d9d3a73 chore: untrack non-data generated artifacts (second pass)
fb04f90e9 chore(weather-engine): commit uncommitted sprint work (dirty-tree reconciliation)
db4223b9a fix(weather-engine): daemon watchdog setsid escape + non-fatal webhook loader
38326d3e8 chore(weather-engine): gitignore *.pem/*.key — never commit private keys
55f788470 fix(weather-engine): R8 runtime fixes — bridge fail-closed + settlement JOIN + auth geocode
04be98a04 chore(weather-engine): reconcile August main-lineage work into recovery lineage
```

Tracked file count: **1058**.

---

## 2. Frozen Config Identity

Config files are **tracked in git** and identified by content hash (sha256) at capture.

| Config file | sha256 | Notes |
|---|---|---|
| `config.json` | `a92c5e1a2b7e3cb145c3442da863ac52773e9d6b7330b62c6c69f4514a0d4e57` | Kalshi API endpoints + spatial_coherence (enabled, boost 0.15, penalty 0.40). `key_id` empty, `has_private_key=false` (no secrets committed). |
| `core/instance_config.py` | `21040dc65d02dff4fe01290b2e3e81154c6c5cab56de556208f74bb5f2d4bd94` | |
| `core/instance_config_fixed.py` | `c67f92b8002d5810608c2966853a10819ab4c57f12fbf0c2ac0b32e7bc89e4fa` | |
| `core/phase1_config.py` | `1af7dfc22ae0a0e5cd6c1156c1c8bf4dc27b1dd57357a417a0b85d0e4741efd0` | |
| `core/signal_config.py` | `fa9e8cc71183c718bc3249fd7c8b27f45df14ef3416591f97e0ffd04ff288029` | |
| `render.yaml` | `602650d19e2e644ad702214c7216899a86fdb6975bbbc05501160a10d2109425` | Render deploy config |
| `scripts/sweep/config.py` | `b713d79e2d9c40f310178f630cdf2d22d28df99b933ff53bb496f34a9494239c` | Sweep config |

**Untracked config** (not in git, captured by content hash only):
| Path | sha256 |
|---|---|
| `config/regime_config_matrix.json` | `d04b7d500ad7b121d3c45025a822395537d34c6b871a44b1f22230ca49db4c12` |

---

## 3. Frozen Data Asset Identity

The `data/` directory is **git-ignored** (`.gitignore` line 16: `data/`) and contains
**live, rapidly-changing** operational databases and caches. These are identified by
**path + sha256 + size + mtime** captured at freeze time. They are *not* versioned in git;
reproducibility of a result that consumed them requires pinning the hash below.

| Data asset | sha256 | Size (bytes) | mtime (UTC) |
|---|---|---|---|
| `data/metar_backfill.db` | `3491680d14cb03b6a0481ef473beb77cbc4775df8bc3f9db71e1a79c022f5aca` | 2471858176 | 2026-09-17 09:51 |
| `data/iem_asos.db` | `ca9c63c07273bd5071630a6dc6b89cb8cd3cddf81bd2de752588de9ebe9087f6` | 663023616 | 2026-09-02 20:31 |
| `data/iem_asos_1min.db` | `51d80ddd986f925739ee380c24ba5550bfe7d4af37941e5db1706bce78602cd9` | 829095936 | 2026-09-17 09:40 |
| `data/ghcn_settlements.db` | `c68bfa0c7e6c6321f486e9e039619c2b122adb6aa33031ff9d878baa86b7bf2b` | 67031040 | 2026-09-14 12:03 |
| `data/gefs_archive.db` | `013840e7e4edc2edc6c004293c600882a1a70c823edbe11b4d9a35afb33bc0bf` | 55132160 | 2026-09-15 03:48 |
| `data/era5_archive.db` | `097c45d77853fd0d357fb2e7387563dd4f868e50c50fe162f409e2efc7e8beab` | 3342336 | 2026-08-31 11:02 |
| `data/kalshi_settlements.db` | `56dca7abd3e1c3e33910b5481c3e6a354d1e4367d06efdee257e249e1d114b75` | 3358720 | 2026-09-17 08:00 |
| `data/kalshi_market_snapshots.db` | `cf59b24dc3c50ee7ffe5f33dc4cf0d48433dd57cc7d85d2a8e1b8c2e4c70865e` | 56434688 | 2026-09-17 09:31 |
| `data/forecast_disagreement_live.db` | `075400f654752b6bcaced05686602fc9341bc3d507cc0e930fb7177d7d9b3edf` | 8671232 | 2026-09-16 11:00 |
| `data/nwp_forecasts.db` | `a15b5b56329c038f9496b9285be7d88727f19254412fe9f45a6c41ebd20ac080` | 324657152 | 2026-09-17 06:45 |
| `data/calibrated_pipeline_v3.pkl` | `598c28419b02f194cd736c2fa3ef9af34ed4c318e28d81feb655e165214e32c5` | 932377 | 2026-07-17 03:49 |
| `data/paper_trading_prod.db` | `94f3cde36a31467da3fb7bf53e49cdcc4292fee2533015235c32bf0167bf85dd` | 77824 | 2026-09-04 01:43 |

**Model artifacts** (calibration pipeline pickles): `calibrated_pipeline.pkl` (1629193 B),
`calibrated_pipeline_v2.pkl` (1624157 B), `calibrated_pipeline_v3.pkl` (932377 B, hashed above).

> ⚠️ Several live DBs (`metar_backfill.db`, `iem_asos_1min.db`, `kalshi_market_snapshots.db`,
> `kalshi_settlements.db`) had mtimes within minutes of capture and are **actively being written**
> by the running daemon. Their hashes are a point-in-time identity, not a stable immutable snapshot.
> For true reproducibility, copy these to a frozen archive before long-running analysis.

---

## 4. Version Identity

- No in-repo `__version__` or `VERSION` constant found in `core/__init__.py` or `config.json`.
- `CHANGELOG.md` latest entry: **`phase2-final - 2026-02-26 (UTC)`**.
- Version is therefore identified by **git commit hash**, not a semantic version tag.

---

## 5. Mutation / Cleanliness Confirmation

- **Code/config/data:** no files were modified, added, deleted, or touched by this freeze.
- **Git status at capture:** working tree has a large set of **untracked** directories/files
  (`data/`, `config/`, `reports/`, `snapshots/`, `artifacts/`, `graphify-*`, `docs/plans/…`, etc.).
  These pre-date this task and were left untouched. No *tracked* file is modified
  (`git status --porcelain` showed only `??` untracked entries; zero ` M ` / ` D ` / ` A ` entries).
- **Live DB / daemon:** not started, stopped, or written by this task.

---

## 6. Reproducibility Statement

A result is reproducible against this baseline when it records:
1. **Code:** commit `04be98a04` (tree `6cbf22904…`), branch `temp-sprint-branch`.
2. **Config:** the 7 tracked config hashes above (plus untracked `config/regime_config_matrix.json`).
3. **Data:** the specific data-asset sha256(s) consumed (see §3), pinned at the moment of ingestion.

Any result produced without these three identities is not traceable to this freeze and must be
re-derived before it can be trusted.
