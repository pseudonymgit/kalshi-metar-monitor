# Evidence Packet — Production Diagnostics (Render)

Generated: 2026-03-03 UTC  
Scope: evidence gathering + diagnostic summary only (no semantic changes to Phase 1 execution paths).

## Environment of collection

- Collection context: **local runtime in this workspace**, not direct Render host log access.
- Endpoint base URL used during active local endpoint probing: `http://127.0.0.1:10000` (local Flask run).  
- Collector default base URL: `http://127.0.0.1:5000` (overridable via `--base-url`).
- Because collection captured repeated `Network is unreachable` errors to Kalshi, findings below reflect an **offline/local diagnostic state** and **cannot certify live production behavior** on Render.

## Commands Run (exact)

```bash
pwd && rg --files -g 'AGENTS.md'
find .. -name AGENTS.md -maxdepth 4
rg --files | head -200
git branch --show-current && git status --short
find . -maxdepth 3 -type d \( -name logs -o -name log -o -name evidence_out \) -print
rg "@app\.route|/observability|runtime-authority|alert-fire|hydration|ingestion-health|discovery|watchlist" app.py core -n
python app.py > /tmp/kalshi_app.log 2>&1 & echo $!
# initial probe on default port (5000); local app was serving on 10000 in this run
sleep 2; curl -sS http://127.0.0.1:5000/observability/ingestion-health | head -c 1000
python app.py > /tmp/kalshi_app.log 2>&1 & echo $! && sleep 2 && tail -n 20 /tmp/kalshi_app.log
# active local probing on actual app port for this run
mkdir -p evidence_out && for ep in ...; do curl -sS "http://127.0.0.1:10000/$ep"; done
python tools/collect_evidence.py
python tools/collect_evidence.py --base-url http://127.0.0.1:10000
python tools/collect_evidence.py > evidence_out/collect_stdout.txt 2> evidence_out/collect_stderr.txt
head -n 40 evidence_out/collect_stderr.txt
cat evidence_out/collect_stdout.txt
rg -n "discovery mismatch|unknown market|TRANSITION_WITHOUT_ALERT|reversion_after_settlement" docs/ROLLING_TODO.md docs/EXECUTION_VISIBILITY_STANDARD.md docs/API_REFERENCE.md
```

## Evidence Artifacts

- Endpoint bundle(s): `evidence_out/bundle_*/evidence_bundle.json`
- Endpoint-only snapshot: `evidence_out/endpoint_snapshots.json`
- Collector stderr with timestamps: `evidence_out/collect_stderr.txt`
- Optional collector script: `tools/collect_evidence.py`

---

## 1) Execution-domain symmetry (no mixed-domain)

### Snapshot evidence
- `GET /execution-domain` returned `200` with `{"execution_domain": "production"}`.
- `GET /observability/runtime-authority-snapshot` returned `200` with `"execution_mode": "observability"`.
- `GET /observability/alert-fire-audit` and `GET /observability/market-coverage` both returned `500` and tracebacks showing:
  - `RuntimeError: Live Kalshi call attempted in forbidden execution domain 'observability'`.

### Finding
- Guardrails are active: observability-domain requests correctly reject live Kalshi calls (no evidence of mixed-domain bypass).
- However, two observability endpoints currently surface 500s under this guard condition, reducing operational visibility.

### Concrete anomaly count
- Forbidden-domain runtime errors observed in this collection run: **2 endpoint failures** (`alert-fire-audit`, `market-coverage`) plus startup/discovery guard errors in stderr.

---

## 2) Hydration/cache stability across stations (flapping/cache_missing)

### Snapshot evidence
From `GET /observability/runtime-authority-snapshot` (`200`):
- Hydration snapshot contains **7 stations**.
- `cache_present: false` for all 7.
- `hydration_prerequisite.cache_valid: false` for all 7.
- `hydration_execution: {}` (empty).

From `GET /observability/hydration-prerequisite-runtime`:
- Returned `400` with `{"error":"station query param required","ok":false}` when no station provided (endpoint functioning as parameterized diagnostic).

### Finding
- No evidence of hydration flapping in this capture; instead, state is uniformly non-hydrated (`cache_valid=false`) across all active stations.
- Current evidence points to persistent cache-miss / non-hydrated posture, not oscillation.

### Concrete anomaly count
- Stations with non-valid hydration prerequisite in snapshot: **7/7**.

---

## 3) Ingestion health across active stations (silent stalls)

### Snapshot evidence
From `GET /observability/ingestion-health` (`200`):
- `scheduler_running: true`
- `last_poll_utc: null`
- station count: 7
- per-station status: **7 stale / 0 fresh**

From `GET /observability/station-summary` (`200`):
- row count: 7
- `ingestion_status: "stale"` for all rows

Timestamped stderr evidence (`collect_stderr.txt`):
- `[2026-03-03 21:29:25,927] ... Series discovery failed during startup ... [Errno 101] Network is unreachable`
- `[2026-03-03 21:29:26,038] ... METAR scheduler loop error ... [Errno 101] Network is unreachable`

### Finding
- This environment shows scheduler alive but ingestion effectively stalled/starved across all active stations.
- Stall is not silent in this run: explicit scheduler/discovery errors are present.

### Concrete anomaly count
- Stale stations: **7/7**.
- Poll timestamp missing: **1 global null (`last_poll_utc`)**.

---

## 4) Discovery integrity (series/watchlist mismatches)

### Snapshot evidence
From `GET /metar/watchlist` (`200`):
- watchlist count: 7 (`KAUS`, `KDEN`, `KLAX`, `KMDW`, `KMIA`, `KNYC`, `KPHL`)

From runtime-authority snapshot:
- hydration station map also present for 7 stations (same active universe size in this capture)

Keyword extraction (collector, repo-local files):
- `discovery mismatch`: 1 hit
- `unknown market`: 1 hit
- `watchlist`: 16 hits

Last-48h context from repo docs:
- `docs/ROLLING_TODO.md` includes explicit open item `Market discovery mismatch investigation` and `Emit alert when unknown market detected`.

### Finding
- No immediate count-size mismatch between watchlist and runtime hydration station map in this snapshot (both 7).
- Discovery risk remains open and acknowledged in backlog; unresolved investigation and unknown-market alerting are still pending.

---

## 5) Transition → alert emission integrity (missed alerts)

### Snapshot evidence
- `GET /observability/transitions` returned `200` with `count: 0`.
- `GET /observability/alert-fire-audit` returned `500` (blocked by observability-domain forbidden live-call path).

Keyword extraction (collector):
- `TRANSITION_WITHOUT_ALERT`: 1 hit (docs)
- `suppressed`: 12 hits (docs/evidence text)
- `alert sent`: 2 hits (docs/evidence text)

### Finding
- In this capture window, no live transitions were emitted (`count=0`), so transition-to-alert integrity cannot be positively certified from runtime rows alone.
- The designated audit endpoint currently fails under guard-triggered discovery path, creating a visibility gap for missed-alert certification in this environment.

---

## Risks / Next Actions (mapped to `docs/ROLLING_TODO.md`)

1. **Discovery-path observability endpoints can fail (500) under forbidden live-call guard.**  
   - Map: `Market discovery mismatch investigation`.
2. **All stations stale while scheduler is running; ingestion stall detection should be first-class.**  
   - Map: `Detect stalled station ingestion automatically`; `Validate scheduler execution per station`.
3. **Unknown-market and transition-without-alert certification is partially blocked by endpoint failure in current environment.**  
   - Map: `Emit alert when unknown market detected`; regression taxonomy item (`hydration, discovery, eligibility, scheduler, execution-domain`).

No execution-domain guards were bypassed in this evidence run.

## LIVE Render Evidence Run — 2026-03-03

- Base URL used: `https://kalshi-metar-monitor.onrender.com` (from `README.md` production URL reference).
- Collector command run: `python tools/collect_evidence.py --base-url https://kalshi-metar-monitor.onrender.com --out-dir evidence_out`
- Bundle path emitted: `evidence_out/bundle_20260304T010059Z/evidence_bundle.json`

### Endpoint highlights

- All 9 probed endpoints in `tools/collect_evidence.py` failed before application response with transport error:
  - `<urlopen error Tunnel connection failed: 403 Forbidden>`
- Direct connectivity check also failed:
  - `curl -sS -D - https://kalshi-metar-monitor.onrender.com/execution-domain`
  - output included `curl: (56) CONNECT tunnel failed, response 403` and `HTTP/1.1 403 Forbidden`.

### Certification categories (LIVE)

- **A) Execution-domain symmetry:** **UNKNOWN** (endpoint unreachable through current network path).
- **B) Hydration / cache stability:** **UNKNOWN** (endpoint unreachable through current network path).
- **C) Ingestion health:** **UNKNOWN** (endpoint unreachable through current network path).
- **D) Discovery integrity:** **UNKNOWN** (endpoint unreachable through current network path).
- **E) Transition → alert integrity:** **UNKNOWN** (endpoint unreachable through current network path).

### Anomaly counts (LIVE run attempt)

- Endpoint transport failures: **9/9**.
- HTTP status from application endpoints: **0 captured** (blocked at CONNECT tunnel stage).

## LIVE Run Blocked

- Blocking condition: egress path from this execution environment to `https://kalshi-metar-monitor.onrender.com` is denied by an HTTP CONNECT tunnel returning `403 Forbidden` before the Render app responds.
- Required to complete LIVE certification snapshot:
  - A network path (or proxy policy) that allows HTTPS CONNECT to `kalshi-metar-monitor.onrender.com`, **or**
  - Execution from a host/environment without the denying proxy in front of outbound HTTPS.
