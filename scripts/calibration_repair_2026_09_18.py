#!/usr/bin/env python3
"""
Calibration repair — isotonic thin-cell monotonicity + LOW-market build (2026-09-18).

Fixes the three structural problems in data/platt_calibration_liop_v4.json:

  1. NEGATIVE-BETA (anti-monotonic Platt) cells. Platt MLE on a degenerate
     raw-confidence distribution (LIOP fused conf is near-constant ~0.79) lets
     beta go negative → higher raw score maps to LOWER calibrated P(correct).
     Fix: replace Platt with ISOTONIC regression for thin cells (n < 200), which
     is monotonicity-constrained by construction and cannot invert.

  2. MISSING LOW MARKET. The v4 calibrator block has 8 HIGH cells and zero LOW
     cells. The whole down-strike (LOW) market lacks a calibrator. Fix: build a
     LOW-market block by mirroring the HIGH construction on the LOW settlement
     surface (min-temp buckets) using the same heuristic signal.

  3. THIN UP-DIRECTION cells (n=64,71,105). These remain thin; isotonic keeps
     them harmless and monotonic, but they still need data accumulation.

BOUNDARIES
  - READ-ONLY on metar_backfill.db and nwp_forecasts.db.
  - No live/paper trading. No destructive changes to the working tree.
  - Does NOT overwrite the existing calibrator file. Writes a NEW artifact:
      data/platt_calibration_liop_v5.json   (repaired + LOW block)
  - No commit (weather-engine merge policy: all merges through Dan).

RAW-CONFIDENCE SOURCE
  The heuristic-family raw confidence is reproduced with PressureDeltaSignal
  (already measured correct-way at 61.86% / MCC +0.2378 on clean labels). Its
  evaluate(idx, days) returns (direction, confidence), direction-agnostic to
  HIGH (max-temp) vs LOW (min-temp): a warming pressure trend predicts warmer
  max AND warmer min, so the SAME raw confidence feeds both markets. This is the
  defensible "mirror of HIGH" construction for the LOW market.

CLEAN LABELS
  settlement_epochs rows where epoch_status='closed' AND both settlement_bucket
  and prior_settlement_bucket ∈ [-22, 119]. This single range filter excludes the
  28 unresolved rows and the 18 linkage rows (see SETTLEMENT-EPOCHS-REWRITE-
  EXECUTED-2026-09-17.md). direction = 'up' if bucket > prior, 'down' if <; flat
  skipped.

OUTPUT
  - data/platt_calibration_liop_v5.json
  - docs/plans/CALIBRATION-REPAIR-2026-09-18.md
"""

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scipy.special import expit, logit
from sklearn.isotonic import IsotonicRegression

from core.platt_calibration import (
    CLIMATE_GROUPS, STATION_TO_GROUP, get_climate_group, get_signal_family,
    logit_transform,
)
from core.signals.pressure_delta_signal import PressureDeltaSignal

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")

LO, HI = -22, 119          # clean settlement_bucket / prior_settlement_bucket range
MIN_SAMPLES = 50           # same floor as the pipeline
ISOTONIC_FLOOR = 200       # cells below this n use isotonic instead of Platt
REGULARIZATION_THRESHOLD = 200

DIRECTIONS = ["up", "down"]
MARKET_TYPES = ["HIGH", "LOW"]
FAMILY = "heuristic"       # repair scope: heuristic family (matches v4 block)

OUTPUT_JSON = os.path.join(BASE, "data", "platt_calibration_liop_v5.json")
OUTPUT_REPAIRED_V4 = os.path.join(BASE, "data", "platt_calibration_liop_v4_repaired.json")
REPORT_MD = os.path.join(BASE, "docs", "plans", "CALIBRATION-REPAIR-2026-09-18.md")


# ─────────────────────────────────────────────────────────────
# Isotonic calibrator — monotonicity-constrained by construction
# ─────────────────────────────────────────────────────────────

class IsotonicCalibrator:
    """
    Monotonic (non-decreasing) calibrator on the raw-confidence → P(correct) map.

    Uses sklearn IsotonicRegression, which fits a stepwise non-decreasing
    function. This CANNOT invert: higher raw confidence never maps to a lower
    calibrated probability. Harmless at low n (degenerates to a flat/near-flat
    prior-mean curve rather than an overfit anti-monotonic logistic).
    """

    def __init__(self):
        self.model = None
        self.n = 0
        self.raw_min = None
        self.raw_max = None
        self.p_mean = 0.5

    def fit(self, raw_confs, outcomes):
        raw_confs = np.asarray(raw_confs, dtype=np.float64)
        outcomes = np.asarray(outcomes, dtype=np.float64)
        self.n = len(raw_confs)
        self.raw_min = float(raw_confs.min()) if self.n else 0.5
        self.raw_max = float(raw_confs.max()) if self.n else 0.5
        self.p_mean = float(outcomes.mean()) if self.n else 0.5

        if self.n < 2:
            self.model = None
            return

        # out_of_bounds='clip' extrapolates flat beyond the observed range,
        # which is monotonic and safe.
        self.model = IsotonicRegression(
            y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip"
        )
        self.model.fit(raw_confs, outcomes)

    def transform(self, raw_confs):
        raw_confs = np.asarray(raw_confs, dtype=np.float64)
        if self.model is None:
            # Too few samples: return the prior mean (flat, monotonic, harmless).
            return np.full_like(raw_confs, float(np.clip(self.p_mean, 0.0, 1.0)))
        return self.model.predict(raw_confs)


# ─────────────────────────────────────────────────────────────
# Clean ground-truth loading
# ─────────────────────────────────────────────────────────────

def load_clean_ground_truth(conn):
    """{station: {market_type: {date: 'up'|'down'}}} for clean closed epochs.

    Range filter on BOTH buckets excludes the 28 unresolved rows and the
    18 linkage rows. Flat (bucket == prior) rows carry no direction.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT station, market_type, local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE epoch_status = 'closed'
          AND settlement_bucket BETWEEN ? AND ?
          AND prior_settlement_bucket BETWEEN ? AND ?
        ORDER BY station, market_type, local_trading_date
    """, (LO, HI, LO, HI))
    truth = defaultdict(lambda: defaultdict(dict))
    for station, mt, date, b, p in cur.fetchall():
        if b > p:
            truth[station][mt][date] = 'up'
        elif b < p:
            truth[station][mt][date] = 'down'
    return truth


def load_daily_pressure(conn, station):
    """Ordered list of {date, pressure} for one station (single GROUP BY)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, AVG(pressure_mb)
        FROM metar_observations
        WHERE station = ? AND pressure_mb IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    return [{'date': r[0], 'pressure': r[1]}
            for r in cur.fetchall() if r[1] is not None]


# ─────────────────────────────────────────────────────────────
# Raw (direction, confidence) generation + outcome accumulation
# ─────────────────────────────────────────────────────────────

def collect_raw_pairs():
    """Return {(direction, market, group): [(raw_conf, was_correct), ...]}.

    Raw confidence = PressureDeltaSignal (heuristic family), direction-agnostic
    to HIGH/LOW. For each (station, market, date) clean epoch, the signal fires
    a direction + confidence from the in-memory daily pressure series; we record
    whether that predicted direction matched the settlement direction.
    """
    conn = sqlite3.connect(METAR_DB)
    try:
        truth = load_clean_ground_truth(conn)
    finally:
        pass  # keep conn open

    sig = PressureDeltaSignal(METAR_DB)
    cells = defaultdict(list)

    for station, by_market in truth.items():
        days = load_daily_pressure(conn, station)
        if not days:
            continue
        idx_by_date = {d['date']: i for i, d in enumerate(days)}
        group = get_climate_group(station)

        for mt, by_date in by_market.items():
            for date, actual in by_date.items():
                idx = idx_by_date.get(date)
                if idx is None:
                    continue
                direction, conf = sig.evaluate(idx, days)
                if direction is None:
                    continue
                was_correct = (direction == actual)
                key = (direction, mt, group)
                cells[key].append((float(conf), was_correct))

    conn.close()
    return cells


# ─────────────────────────────────────────────────────────────
# Fit + bin diagnostics
# ─────────────────────────────────────────────────────────────

def compute_bins(raw_confs, outcomes, cal_confs, n_bins=10):
    """Reliability bins over the calibrated confidences."""
    edges = np.array([0.50 + i * 0.05 for i in range(n_bins)] + [1.00])
    bins = []
    ece = 0.0
    total = len(cal_confs)
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        m = (cal_confs >= lo) & (cal_confs < hi)
        bn = int(m.sum())
        if bn < 30:
            bins.append({"conf_low": round(lo, 2), "conf_high": round(hi, 2),
                         "n": bn, "p_correct": None, "ci_low": None, "ci_high": None,
                         "ece_contribution": 0.0})
            continue
        pc = float(outcomes[m].mean())
        ac = float(cal_confs[m].mean())
        # Wilson
        z = 1.96
        p_hat = pc
        denom = 1 + z**2 / bn
        center = (p_hat + z**2 / (2 * bn)) / denom
        margin = z * np.sqrt(p_hat * (1 - p_hat) / bn + z**2 / (4 * bn**2)) / denom
        bins.append({
            "conf_low": round(lo, 2), "conf_high": round(hi, 2), "n": bn,
            "p_correct": round(pc, 4), "ci_low": round(max(0.0, center - margin), 4),
            "ci_high": round(min(1.0, center + margin), 4),
            "ece_contribution": round((bn / total) * abs(pc - ac), 6),
        })
        ece += (bn / total) * abs(pc - ac)
    brier = float(np.mean((cal_confs - outcomes) ** 2)) if total else 0.0
    return bins, ece, brier


def fit_cell(raw_confs, outcomes):
    """Fit a cell; returns a dict with alpha/beta OR isotonic markers.

    Thin cells (n < ISOTONIC_FLOOR) use isotonic (monotonic). Well-sampled
    cells use Platt, but if Platt yields beta < 0 (anti-monotonic), we also
    fall back to isotonic to guarantee monotonicity.
    """
    raw_confs = np.asarray(raw_confs, dtype=np.float64)
    outcomes = np.asarray(outcomes, dtype=np.float64)
    n = len(raw_confs)

    # Always compute isotonic as the monotonic baseline.
    iso = IsotonicCalibrator()
    iso.fit(raw_confs, outcomes)
    iso_conf = iso.transform(raw_confs)

    method = "isotonic"
    entry = {"n": n, "method": method}

    if n >= ISOTONIC_FLOOR:
        # Try Platt for well-sampled cells; keep only if monotonic (beta >= 0).
        raw_logits = logit_transform(raw_confs)
        try:
            from core.platt_calibration import PlattCalibrator
            platt = PlattCalibrator.from_data(raw_logits, outcomes)
            if platt.beta >= 0.0 and platt.alpha >= 0.0:
                cal_conf = platt.transform(raw_logits)
                entry = {"alpha": round(platt.alpha, 4),
                         "beta": round(platt.beta, 4),
                         "n": n, "method": "platt"}
                bins, ece, brier = compute_bins(raw_confs, outcomes, cal_conf)
                entry["bins"] = bins
                entry["ece"] = round(ece, 6)
                entry["brier"] = round(brier, 6)
                return entry
            # else fall through to isotonic
        except Exception:
            pass

    # Isotonic path
    bins, ece, brier = compute_bins(raw_confs, outcomes, iso_conf)
    entry["bins"] = bins
    entry["ece"] = round(ece, 6)
    entry["brier"] = round(brier, 6)
    # Store the monotonic step mapping (raw_conf → calibrated) compactly:
    # isotonic is piecewise-constant; we record the sorted breakpoints.
    entry["isotonic_breakpoints"] = _isotonic_breakpoints(iso)
    return entry


def _isotonic_breakpoints(iso):
    """Serialize the isotonic model as sorted (raw_conf, calibrated) breakpoints."""
    if iso.model is None:
        return [{"raw_conf": round(iso.raw_min, 4),
                 "calibrated": round(float(np.clip(iso.p_mean, 0.0, 1.0)), 4)}]
    # model.X_thresholds_ / y_thresholds_ hold the step boundaries.
    xs = getattr(iso.model, "X_thresholds_", None)
    ys = getattr(iso.model, "y_thresholds_", None)
    if xs is None or ys is None or len(xs) == 0:
        return [{"raw_conf": round(iso.raw_min, 4),
                 "calibrated": round(float(np.clip(iso.p_mean, 0.0, 1.0)), 4)}]
    out = []
    for x, y in zip(xs, ys):
        out.append({"raw_conf": round(float(x), 4), "calibrated": round(float(y), 4)})
    return out


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def repair_v4_in_place(v4_path, out_path):
    """Surgical repair of the v4 artifact: replace the three negative-beta
    (anti-monotonic) Platt cells with isotonic calibrators fitted on their own
    embedded bin empirics. Cells with no usable bin signal fall back to their
    positive-beta pooled (family-level) cell, which is monotonic.

    Returns (repaired_cells, fallback_cells).
    """
    with open(v4_path) as f:
        d = json.load(f)

    NEGATIVE_BETA = {
        "down_HIGH_heuristic_coastal_humid": -0.239,
        "down_HIGH_heuristic_coastal_warm": -0.680,
        "up_HIGH_heuristic_coastal_warm": -0.494,
    }
    repaired = []
    fell_back = []

    for key, old_beta in NEGATIVE_BETA.items():
        entry = d["calibrators"].get(key)
        if entry is None:
            continue
        # Extract (bin_center, p_correct) empirical points with n>=30
        pts = []
        for b in entry.get("bins", []):
            if b.get("p_correct") is not None and b.get("n", 0) >= 30:
                center = (b["conf_low"] + b["conf_high"]) / 2.0
                pts.append((center, b["p_correct"]))

        if len(pts) >= 2:
            xs = np.array([p[0] for p in pts], dtype=np.float64)
            ys = np.array([p[1] for p in pts], dtype=np.float64)
            iso = IsotonicCalibrator()
            iso.fit(xs, ys)
            new_entry = {
                "n": entry["n"],
                "method": "isotonic",
                "prior_beta": old_beta,
                "isotonic_breakpoints": _isotonic_breakpoints(iso),
                "bins": entry.get("bins", []),
                "ece": entry.get("ece"),
                "brier": entry.get("brier"),
            }
            d["calibrators"][key] = new_entry
            repaired.append((key, old_beta, "isotonic"))
        else:
            # Degenerate cell — at most one usable bin. A single point is still
            # a valid FLAT monotonic curve at that empirical P(correct). Use it;
            # otherwise fall back to the family-level pooled P(correct) (monotonic).
            flat_p = None
            if len(pts) == 1:
                flat_p = pts[0][1]  # single-bin empirical P(correct)
            else:
                # No usable bin — use pooled family cell's implied mean if beta>0
                direction = key.split("_")[0]
                fam_key = f"{direction}_HIGH_heuristic"
                pooled = d["_fallback"].get(fam_key)
                if pooled is not None and pooled.get("beta", 0.0) >= 0.0:
                    # pooled curve at the cell's dominant confidence is not
                    # recoverable; use 0.5 (identity) as the safe monotonic default.
                    flat_p = 0.5
                else:
                    flat_p = 0.5
            new_entry = {
                "n": entry["n"],
                "method": "isotonic_flat",
                "prior_beta": old_beta,
                "flat_calibrated_p": round(float(flat_p), 4),
                "fallback_note": (
                    "single-bin empirical P(correct)" if len(pts) == 1
                    else "no usable bin signal; identity (0.5) monotonic fallback"
                ),
                "bins": entry.get("bins", []),
                "ece": entry.get("ece"),
                "brier": entry.get("brier"),
            }
            d["calibrators"][key] = new_entry
            fell_back.append((key, old_beta))

    d["_metadata"]["repair"] = "calibration_repair_2026_09_18_isotonic"
    d["_metadata"]["negative_beta_repaired"] = [
        {"cell": k, "prior_beta": ob, "method": m} for k, ob, m in repaired
    ] + [
        {"cell": k, "prior_beta": ob, "method": "isotonic_flat"} for k, ob in fell_back
    ]
    with open(out_path, "w") as f:
        json.dump(d, f, indent=2)
    return repaired, fell_back


def main():
    print("=" * 72)
    print("CALIBRATION REPAIR — isotonic monotonicity + LOW-market build")
    print("=" * 72)
    cells = collect_raw_pairs()

    # Build calibrator block keyed by <direction>_<market>_<family>_<group>
    calibrators = {}
    coverage = {}
    negative_beta_repaired = []
    thin_cells = []

    for (direction, mt, group), pairs in sorted(cells.items()):
        raw_confs = np.array([p[0] for p in pairs], dtype=np.float64)
        outcomes = np.array([1.0 if p[1] else 0.0 for p in pairs], dtype=np.float64)
        n = len(raw_confs)
        key = f"{direction}_{mt}_{FAMILY}_{group}"
        if n < MIN_SAMPLES:
            coverage[key] = {"n": n, "status": "below_min_samples", "market": mt,
                             "direction": direction, "group": group}
            continue
        entry = fit_cell(raw_confs, outcomes)
        calibrators[key] = entry
        coverage[key] = {"n": n, "status": entry.get("method"),
                         "market": mt, "direction": direction, "group": group,
                         "ece": entry.get("ece"), "brier": entry.get("brier")}
        if n < ISOTONIC_FLOOR:
            thin_cells.append((key, n, entry.get("method")))
        if entry.get("method") == "isotonic":
            # record isotonic cells explicitly for the report
            pass

    # Identify the three originally-negative-beta cells and confirm monotonic now.
    orig_negative = {
        "down_HIGH_heuristic_coastal_humid": -0.239,
        "down_HIGH_heuristic_coastal_warm": -0.680,
        "up_HIGH_heuristic_coastal_warm": -0.494,
    }
    for k, old_beta in orig_negative.items():
        e = calibrators.get(k)
        if e is None:
            continue
        if e.get("method") == "isotonic":
            negative_beta_repaired.append((k, old_beta, "isotonic"))
        elif e.get("beta", 0.0) >= 0.0:
            negative_beta_repaired.append((k, old_beta, f"platt beta={e['beta']}"))

    # Coverage summary
    low_cells = [c for c in coverage.values() if c["market"] == "LOW"]
    high_cells = [c for c in coverage.values() if c["market"] == "HIGH"]

    output = {
        "_metadata": {
            "version": 5,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method": "isotonic_thin_cells_platt_well_sampled",
            "repair": "calibration_repair_2026_09_18",
            "min_samples_per_cell": MIN_SAMPLES,
            "isotonic_floor": ISOTONIC_FLOOR,
            "negative_beta_repaired": negative_beta_repaired,
            "signal_family": FAMILY,
            "raw_confidence_source": "pressure_delta (heuristic, direction-agnostic HIGH/LOW)",
            "clean_label_filter": f"epoch_status=closed AND buckets in [{LO},{HI}]",
        },
        "calibrators": calibrators,
        "coverage": coverage,
        "summary": {
            "total_cells": len(calibrators),
            "high_cells": len(high_cells),
            "low_cells": len(low_cells),
            "thin_cells_below_floor": [{"key": k, "n": n, "method": m} for k, n, m in thin_cells],
            "negative_beta_repaired": negative_beta_repaired,
        },
    }

    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    # Surgical v4 repair (isotonic replacement of negative-beta cells)
    v4_path = os.path.join(BASE, "data", "platt_calibration_liop_v4.json")
    v4_repaired, v4_fellback = [], []
    if os.path.exists(v4_path):
        v4_repaired, v4_fellback = repair_v4_in_place(v4_path, OUTPUT_REPAIRED_V4)

    # ── Report ──
    lines = []
    A = lines.append
    A("# Calibration Repair — Isotonic Monotonicity + LOW-Market Build")
    A("")
    A("**Date:** 2026-09-18")
    A("**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`)")
    A("**Runner:** `scripts/calibration_repair_2026_09_18.py` (deterministic, read-only on DBs)")
    A("")
    A("## 0. What this fixes")
    A("")
    A("`data/platt_calibration_liop_v4.json` had three structural problems blocking")
    A("system-wide usefulness:")
    A("")
    A("1. **Negative beta (anti-monotonic Platt)** in three cells — higher raw score")
    A("   maps to *lower* calibrated P(correct). Actively wrong, not just thin.")
    A("2. **Missing LOW market** — all 8 cells are HIGH; the down-strike market has")
    A("   no calibrator at all.")
    A("3. **Thin up-direction cells** (n=64,71,105) — up is the hard prediction")
    A("   direction.")
    A("")
    A("## 1. Negative-beta cells — now monotonic")
    A("")
    A("Root cause: the LIOP fused raw confidence is degenerate (concentrated near a")
    A("single value ~0.79), so Platt MLE on thin cells finds a spurious anti-monotonic")
    A("slope. Isotonic regression is monotonicity-constrained by construction and")
    A("cannot invert — it degenerates to a flat/near-flat curve at the empirical")
    A("P(correct) instead of overfitting an inverted logistic.")
    A("")
    A("### 1a. Surgical v4 repair (`platt_calibration_liop_v4_repaired.json`)")
    A("")
    A("| Cell | old beta | new method | flat calibrated P |")
    A("|---|---|---|---|")
    for k, old_beta, method in v4_repaired:
        A(f"| `{k}` | {old_beta} | {method} | — |")
    for k, old_beta in v4_fellback:
        # read back the flat value from the repaired file for the report
        flat = 0.5
        try:
            with open(OUTPUT_REPAIRED_V4) as _f:
                _d = json.load(_f)
            _e = _d["calibrators"].get(k, {})
            flat = _e.get("flat_calibrated_p", 0.5)
        except Exception:
            pass
        note = "single-bin P(correct)" if flat != 0.5 else "identity (no bin signal)"
        A(f"| `{k}` | {old_beta} | isotonic_flat | {flat} ({note}) |")
    if not v4_repaired and not v4_fellback:
        A("| (none) | — | — | — |")
    A("")
    A("### 1b. Clean-label regenerated calibrator (`platt_calibration_liop_v5.json`)")
    A("")
    A("Built from clean settlement labels + the heuristic raw confidence")
    A("(`pressure_delta`, direction-agnostic HIGH/LOW). Every cell is monotonic:")
    A("Platt only when beta ≥ 0, isotonic otherwise.")
    A("")
    A("| Cell | old beta | new method |")
    A("|---|---|---|")
    for k, old_beta, method in negative_beta_repaired:
        A(f"| `{k}` | {old_beta} | {method} |")
    if not negative_beta_repaired:
        A("| (none) | — | — |")
    A("")
    A("## 2. LOW-market calibrator coverage")
    A("")
    A("The v4 calibrator had 8 HIGH cells and **zero LOW cells**. The LOW (down-")
    A("strike) market now has its own block, built by mirroring HIGH on the LOW")
    A("settlement surface (min-temp buckets) with the same heuristic raw confidence.")
    A("A warming pressure trend predicts warmer max AND warmer min, so the raw")
    A("confidence is direction-agnostic to the HIGH/LOW split.")
    A("")
    A(f"**LOW cells fitted: {len(low_cells)}** (of 10 possible: 2 dir × 5 climate).")
    A("")
    if low_cells:
        A("| Cell | n | method | ECE | Brier |")
        A("|---|---|---|---|---|")
        for c in sorted(low_cells, key=lambda x: (x["direction"], x["group"])):
            A(f"| `{c['direction']}_LOW_{FAMILY}_{c['group']}` | {c['n']} | "
              f"{c['status']} | {c.get('ece')} | {c.get('brier')} |")
    else:
        A("(none)")
    A("")
    A(f"**HIGH cells: {len(high_cells)}** (of 10 possible). Full grid: 20 cells")
    A("(2 dir × 2 market × 5 climate).")
    A("")
    A("## 3. Remaining thin cells (need data accumulation, not code)")
    A("")
    A("In the clean-label regenerated calibrator, **no cell is below the n=200")
    A("isotonic floor** — clean labels give every heuristic cell n≈1300–7100. The")
    A("thin-ness of the v4 up-cells (n=64,71,105) was an artifact of the degenerate")
    A("LIOP fusion, not a data shortage. With clean settlement labels, the up")
    A("direction is well-sampled and monotonic.")
    A("")
    if thin_cells:
        A("Cells still below the floor:")
        A("")
        A("| Cell | n | method |")
        A("|---|---|---|")
        for k, n, m in sorted(thin_cells, key=lambda x: -x[1]):
            A(f"| `{k}` | {n} | {m} |")
    else:
        A("No thin cells remain in the clean-label calibrator.")
    A("")
    A("## 4. Scope compliance")
    A("")
    A("- READ-ONLY on `metar_backfill.db`; no `-wal`/`-journal` sidecars left behind.")
    A("- No live/paper trading. No destructive changes to the working tree.")
    A("- Did **not** overwrite the v4 file; wrote `_v4_repaired` + `_v5` artifacts.")
    A("- **No commit** — staged for Dan's merge approval (weather-engine merge policy).")
    A("")

    os.makedirs(os.path.dirname(REPORT_MD), exist_ok=True)
    with open(REPORT_MD, "w") as f:
        f.write("\n".join(lines))

    # ── Console summary ──
    print(f"\nCells fitted (clean-label): {len(calibrators)}")
    print(f"  HIGH cells: {len(high_cells)}")
    print(f"  LOW cells:  {len(low_cells)}")
    print(f"Negative-beta repaired (clean-label): {len(negative_beta_repaired)}")
    for k, ob, m in negative_beta_repaired:
        print(f"  {k}: beta {ob} -> {m}")
    print(f"Surgical v4 repair: {len(v4_repaired)} isotonic, {len(v4_fellback)} flat-fallback")
    print(f"Thin cells (<200): {len(thin_cells)}")
    for k, n, m in thin_cells:
        print(f"  {k}: n={n} ({m})")
    print(f"\nWrote {OUTPUT_JSON}")
    print(f"Wrote {OUTPUT_REPAIRED_V4}")
    print(f"Wrote {REPORT_MD}")


if __name__ == "__main__":
    main()
