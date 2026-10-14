# PWW Bug Hunt — Fresh Pass (2026-10-14)

**Repo:** `prototypes/weather-engine-source/`
**Author:** Gilfoyle · **Workstream AG-2**
**Scope:** `scripts/sbox_intraday_ev_bridge.py` (post-surgery: om_gfs sole + Kelly),
`scripts/low_entry_bridge.py` (LOW lane), `core/intraday/om_gfs_nwp.py` (Tmin path),
`core/intraday/honest_calibration.py`, `scripts/bullshit_crawl.py` + `_v2.py`
**Rule:** every claim is traceable to a file:line or a specific query on a
read-only DB. No DB writes. Format = the Sprint-1 PWW format (verdict first).

---

## VERDICT — **CONDITIONAL**

The **HIGH-lane core is trustworthy**: Kelly math is correct and capped, the
calibration is two-sided and fails closed, the EV gate fails closed, and the
settlement/pin path is honest. **But the LOW lane has one material blocker**
(the Tmin bias silently reads the wrong cache axis → a +5°F capped bias instead
of −1.17°F) plus a missing running-low truncation, and the shipped P&L **basis
label** is stale relative to the code. The LOW lane is **not** wired
(`LOW_BRIDGE_ENABLED` is a dead variable), so nothing is corrupting live data —
but it must not be armed until F1/F2/F4 are fixed.

**Do not arm `LOW_BRIDGE_ENABLED` until F1 and F4 are fixed.**

---

## Findings

| # | severity | file:line | what's wrong | why it matters | fix |
|---|---|---|---|---|---|
| **F1** | **HIGH** | `core/intraday/om_gfs_nwp.py:359,369` | `rolling_bias_f(market_type="LOW")` reads the **Tmax** cache key. The LOW fetch stores under `hist\|temperature_2m_min` (line 273) but the bias lookup uses `_cache_key(station, ds, "hist")` — no `src`/namespace. | The LOW bias becomes `Tmax_forecast − Tmin_truth` = **+17.75°F** pooled, clamped to the **+5.0** cap, instead of the correct **−1.17°F**. `mu = Tmin − bias` is then ~6°F too low → every LOW strike's `p_ge` shifts → wrong strike, wrong EV. **Silent**: no warning, returns a plausible number. | `src = "hist" if market_type!="LOW" else "hist|temperature_2m_min"`; use `_cache_key(station, ds, src)` at lines 359/369 (and the `live` fallback at 371). |
| **F2** | **HIGH** | `scripts/low_entry_bridge.py:110,206-233` | The LOW lane **never applies the running-low truncation** the design mandates. `sole_tmin_p_fn` returns `1 − state.p_ge(x)` with **no** clamp to `P(min≤X)=1 for X ≤ running_low@pin`; `select_strike` is called with `state.mu, state.sigma` (Gaussian) and NO `p_fn` truncation hook. | The whole point of the LOW axis (mirror of HIGH `high_so_far` truncation, `LOW-MARKET-LANE-DESIGN §4.1`) is that a strike already at/below the observed low is a **locked win**. Without it the lane misprices locked wins as uncertain and can pick the wrong strike/leg. Design-doc/reality divergence. | Build the LOW state with `low_so_far = min(ASOS buckets ≤ pin)` and enforce `p_min_le(x)=1.0 for x ≥ low_so_far − ε` in `sole_tmin_p_fn`, or extend `DistributionState` with a `low_so_far` truncation mirror. |
| **F3** | **HIGH** | `scripts/low_entry_bridge.py:219-220` | LOW picks strikes with `fail_closed=False` and **no `cost_model`**, so EV is priced off the bare mid. The HIGH path uses `fail_closed=True` + a station spread model. | Strikes that are only +EV on the mid survive once the ~0.0205 execution cost is real — a systematic **over-entry** bias vs HIGH, and a silent fallback (fail-closed→fail-open). | Pass `cost_model=_station_spread_model(ladder)` and `fail_closed=True`, mirroring `sbox_intraday_ev_bridge.py:1055-1057`. |
| **F4** | **MED** | `scripts/low_entry_bridge.py:64` | `LOW_BRIDGE_ENABLED` is **read once and never used** (grep: only definition + docstring). The lane has **no arming gate**. | The "prepare-only, defaults OFF" safety claim is **not enforced in code**: any accidental import/run of `run()` books LOW rows. The flag is decorative. | Guard `run()` (or `record_trade` call) on `LOW_BRIDGE_ENABLED`; or raise unless `--dry-run`. |
| **F5** | **MED** | `scripts/low_entry_bridge.py:278` vs `scripts/sbox_intraday_ev_bridge.py:1140-1160` | LOW skips the **F9 exposure gate** (`exposure_skip_reason`), the **GR5 booking gate** (`admit_decision_candidate`) and the **ticker-uniqueness** backstop. HIGH applies all three. | LOW and HIGH co-exist in one book (`lane` column) but LOW bypasses the portfolio caps → the shared book's exposure limits are not honored for LOW. | Mirror the HIGH book-gate block before `record_trade`. |
| **F6** | **MED** | `scripts/sbox_intraday_ev_bridge.py:1039` + `:891-916` | On the **sole** path, `select_nwp_state` silently falls back to the raw local GFS-seeded WS-P (`nwp_src="gfs_local"`) when om_gfs is unavailable, but `build_om_gfs_sole_p_fn` treats whatever state it is handed as om_gfs and the companion row is stamped `fusion_rule=om_gfs_sole`. | A stale/absent om_gfs silently books a **GFS-seeded** forecast under an **om_gfs** provenance label — a plausible number with the wrong source, and the exact failure mode the "sole forecast" surgery was meant to remove. | On the sole path, require `nwp_src == "om_gfs"`; if not, skip the station (`skipped.append((st,"sole_no_om_gfs"))`) or fail closed. |
| **F7** | **MED** | `docs/plans/WE-COMPLETE-REGISTER-2026-10-13.md` (AD-3, AF banner) vs book | Register claims "constant-1-contract" and "78/78 closed rows match ·1, 0 match ·qty". The shipped book's `realized_pnl` is **qty-scaled**: 43/43 closed rows match `qty·(S−trade_price)`; **0/43** match constant-1. | The register's stated basis contradicts the code's output. Metrics/dashboards repointed off the wrong label report a number that is off by ~×qty. | Correct the label: the going-forward basis is **contract-notional dollars** (qty-scaled); constant-1 is retired. (Item 3 does this.) |
| **F8** | **LOW** | `scripts/bullshit_crawl.py:31` | `MAX_POSITION_USD = 25.0` — the book's cap is now **$10** (AF-2 Kelly; `GRADE_POSITION_CAP_USD` max S=$25, `MAX_POSITION_USD=10`). | R2 "over max position" can never fire, so the crawl silently under-reports over-size rows. Stale threshold, not a wrong number. | Set to `10.0` (or import the single source). |
| **F9** | **LOW** | `scripts/bullshit_crawl_v2.py:79` | `"generated_at_utc": None` — v2 never stamps its run time, while v1 does (`datetime.now`). | The v2 artifact cannot be ordered vs the book state it describes; a stale v2 JSON is indistinguishable from fresh. | Write `datetime.now(timezone.utc).isoformat()`. |
| **F10** | **LOW** | `core/intraday/honest_calibration.py:88-92` | `honest_calibrated_p(0.0, ce)` returns **0.1175** (the `max(0.01, v)` floor is applied). A maximally-bearish raw signal still yields 11.75% YES. | Asymmetric floor: the model can never price below 0.01, but the *calibrated* value bottoms out at ~0.1175 for a 0-raw. Confirmed by direct call. Intentional? If not, the floor is manufacturing a small floor-YES. | Document the floor as intended, or lower it to the raw floor (0.01) — but only if the floor was not load-bearing. |
| **F11** | **INFO** | `scripts/low_entry_bridge.py:167,171` | `low_ladder_from_csv` sets `no_price = 1 − mid` from the B-row YES mid; `volume_24h`/`snapshot_time` are hardcoded `None`, so the F1 top-liquidity spread probe silently falls back to the 0.0205 constant. | The LOW NO leg is a synthetic complement, not a real NO mid (the backfill's own `low_market_price_backfill_2026-10-13.json` reports proxy error median 0.18, high-moneyness median 0.83). | Price the NO leg from its own row when present; keep the synthetic complement only as an explicit fallback. |

---

## What's clean (verified working — not only negatives)

- **Kelly math is correct and the caps hold** (`sbox_intraday_ev_bridge.py:489-521`).
  Verified by direct call: `f*=(p−m)/m`; `(0.6,0.5)→0.20→$10`; `(0.99,0.05)→18.8`
  capped to the 5% fraction → `$10`; degenerate `m∈{0,1}` → `0.0` (no divide-by-zero);
  `p≤m` → `0.0`. Loss side ≤ $10 always. No 19:1 asymmetry.
- **`honest_calibrated_p` is two-sided and neutral at 0.5** (`honest_calibration.py`).
  `honest_calibrated_p(0.5, 0.82) == 0.5` exactly; NaN → `ValueError` (callers fail
  closed). The one-sided `+0.0705` coin-flip bug (AD-2) is genuinely gone.
- **HIGH EV gate fails closed** on a missing/0/NaN `calibrated_p`
  (`sbox_intraday_ev_bridge.py:1067-1070`) — no entry without a usable honest p.
- **Settlement uses real per-station settlement instants** — `_hours_before_settlement`
  (`:1490`) resolves the station-local midnight; `run()` (`:952-957`) uses it with a
  6.0 fallback only when unresolvable. The old hardcoded 6 is gone from the gate.
- **LOW forecast axis is namespaced correctly** (`om_gfs_nwp.py:266-273`): the Tmin
  fetch caches under `hist\|temperature_2m_min`, so a LOW caller can never be served a
  Tmax value. (Only the **bias** reader, F1, misses the namespace.)
- **LOW `p_ge→p_low_le` flip is correct**: `P(min≤X) = 1 − p_ge(X)` where the Tmin
  state's `p_ge(X)=P(min≥X)`; increasing in X. No axis inversion in the LOW pricing.
- **The book is a blank slate**: current `data/paper_trading_sbox.db` = **0 rows**
  (`SELECT COUNT(*) FROM trades` → 0), 15 tables intact. No stale/saturated rows.
- **`bullshit_crawl.py` is honest about its own limits**: it flags 98.8% on the old
  book and states the flagged fraction is a *signal-shape* finding, not a sizing one
  (`BULLSHIT-CRAWL-SPRINT1-GATE-2026-10-13.md`).

---

## Reproducible (every claim)

```sql
-- F7: basis is qty-scaled, not constant-1 (43/43 vs 0/43), read-only backup
-- data/paper_trading_sbox.db.pre_straggler_fix_20261014T230128Z
-- assert abs(realized_pnl - qty*(S - trade_price)) < 1e-3  -> 43/43 rows
-- assert abs(realized_pnl - (S - trade_price))       < 1e-3  ->  0/43 rows
-- (id 193: qty=87, trade_price=0.115, S=0 -> -10.005; constant-1 would be -0.115)
```

```bash
# F1: LOW bias reads the Tmax axis -> +17.75F pooled, capped +5.0 (vs correct -1.17F)
python3 - <<'PY'
import json,sqlite3,statistics
d=json.load(open('data/_omgfs_nwp_cache.json'))
c=sqlite3.connect('file:data/metar_backfill.db?mode=ro',uri=True)
t={(r[0],r[1]):float(r[2]) for r in c.execute(
  "SELECT station,local_trading_date,settlement_temp_f FROM settlement_truth "
  "WHERE market_type='LOW' AND settlement_temp_f IS NOT NULL AND (trustworthy IS NULL OR trustworthy=1)")}
coded=[d[f'{s}|{ds}|hist']-v for (s,ds),v in t.items() if f'{s}|{ds}|hist' in d]     # BUG path
ok   =[d[f'{s}|{ds}|hist|temperature_2m_min']-v for (s,ds),v in t.items()
       if f'{s}|{ds}|hist|temperature_2m_min' in d]                                   # correct
print('coded mean', round(statistics.mean(coded),2), '-> cap +5.0')
print('correct mean', round(statistics.mean(ok),2))
PY

# F10: calibrated floor
python3 -c "from core.intraday.honest_calibration import honest_calibrated_p as h; \
print(h(0.5,0.82), h(0.0,0.82), h(0.99,0.82))"   # 0.5 / 0.1175 / 0.82

# F2/F3/F4/F5: grep confirms the missing truncation / cost model / dead flag / gates
grep -n "running_low\|rlow\|low_so_far" scripts/low_entry_bridge.py   # -> 0 hits (comments only)
grep -rn "LOW_BRIDGE_ENABLED" scripts/ core/                          # -> def + docstring only
grep -n "cost_model\|fail_closed\|exposure_skip_reason\|admit_decision_candidate" \
     scripts/low_entry_bridge.py                                      # -> none on the LOW booking path

# F8/F9: stale thresholds + missing stamp
grep -n "MAX_POSITION_USD" scripts/bullshit_crawl.py                 # -> 25.0 (book cap is 10)
grep -n "generated_at_utc" scripts/bullshit_crawl_v2.py              # -> None
```

---

## Bottom line for Dan

The HIGH lane is finally honest — Kelly, calibration, EV gate, and settlement all
check out. The sentence to strike is **"the LOW lane is a HIGH mirror"**: it is a
*partial* mirror. It drops the running-low truncation (the LOW-specific core), the
execution-cost model, and the whole booking/exposure gate stack, and its bias
reader is wired to the Tmax axis (**F1**). Because `LOW_BRIDGE_ENABLED` is dead
code (**F4**), none of this touches live data today — but the previous "prepare
only, safe by default" claim was **not** true in code. Fix F1–F5 before arming.
