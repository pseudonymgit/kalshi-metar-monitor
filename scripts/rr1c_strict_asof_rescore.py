#!/usr/bin/env python3
"""
R/R-1c — strict as-of re-score of nwp_direct + nwp_dtdt_fusion.

The strict fetch_timestamp gate + mixed-units hygiene are now INSIDE
NwpDirectSignal._get_temp (committed as part of this change), so this script
scores the signals as-is and the strict gate is enforced automatically.

Axis: strike-crossing (settlement_temp_f vs strike_price).  NOT day-over-day.

Deterministic / script-only.  NO AI calls inside the loop.  Read-only against DBs.

Every number carries axis + provenance.  Base rate up=34.36% (down-skewed);
always-down null Sharpe ~5.25; MCC ~ 0 = artifact.

Output: reports/rr1c_strict_asof_rescore_<ts>.json + console table.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.signals.nwp_direct_signal import NwpDirectSignal
from core.signals.nwp_dtdt_fusion_signal import NwpDtdtFusionSignal
from core.signals.metar_dtdt_signal import MetarDtdtSignal

METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
REPORTS_DIR = os.path.join(BASE, "reports")

import scripts.ultimate_sweep as us


def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _sharpe(returns):
    if not returns:
        return None
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n if n > 1 else 0.0
    sd = var ** 0.5
    if sd == 0:
        return None
    return (mean / sd) * (252 ** 0.5)


def _score_one(sig, name, labels, meta, conn, pass_strike=True):
    stats = {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0}
    returns = []
    per_mt = {}
    for st, lblmap in labels.items():
        days = us.load_daily_series(conn, st)
        if not days:
            continue
        idx_by_date = {d['date']: i for i, d in enumerate(days)}
        for (mt, date), actual in lblmap.items():
            idx = idx_by_date.get(date)
            if idx is None:
                continue
            strike = meta[st][(mt, date)]['strike'] if pass_strike else None
            try:
                if name == 'nwp_direct':
                    direction, confidence = sig.evaluate_for_station(
                        st, date, market_type=mt, strike_price=strike)
                elif name == 'nwp_dtdt_fusion':
                    direction, confidence = sig.evaluate_for_station(
                        st, date, conn=conn, market_type=mt, strike_price=strike)
                elif name == 'metar_dtdt':
                    direction, confidence = sig.evaluate_for_station(
                        st, date, conn=conn, market_type=mt)
                else:
                    direction, confidence = None, 0.0
            except Exception:
                direction, confidence = None, 0.0
            if direction is None:
                continue
            if direction == 'up':
                if actual == 'up':
                    stats['tp'] += 1
                    per_mt.setdefault(mt, {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})['tp'] += 1
                else:
                    stats['fp'] += 1
                    per_mt.setdefault(mt, {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})['fp'] += 1
            else:
                if actual == 'down':
                    stats['tn'] += 1
                    per_mt.setdefault(mt, {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})['tn'] += 1
                else:
                    stats['fn'] += 1
                    per_mt.setdefault(mt, {'tp': 0, 'fp': 0, 'tn': 0, 'fn': 0})['fn'] += 1
            returns.append(1.0 if direction == actual else -1.0)

    tp, fp, tn, fn = stats['tp'], stats['fp'], stats['tn'], stats['fn']
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    null_correct = 0
    for mt in per_mt:
        m = per_mt[mt]
        null_correct += max(m['tp'] + m['fn'], m['fp'] + m['tn'])
    null_acc = null_correct / total if total else 0.0
    sharpe = _sharpe(returns)
    return {
        'accuracy': round(acc, 6),
        'mcc': round(_mcc(tp, fp, tn, fn), 6),
        'n': total,
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
        'base_rate_accuracy': round(null_acc, 6),
        'base_rate_delta': round(acc - null_acc, 6),
        'sharpe': round(sharpe, 6) if sharpe is not None else None,
        'n_returns': len(returns),
        'up_ratio_predicted': round((tp + fp) / total, 4) if total else None,
    }


def main():
    t0 = time.time()
    labels, src_map, meta, excl = us.load_labels()

    conn = sqlite3.connect(METAR_DB)

    sigs = {
        'nwp_direct': NwpDirectSignal(db_path=NWP_DB),
        'nwp_dtdt_fusion': NwpDtdtFusionSignal(db_path=METAR_DB, nwp_db_path=NWP_DB),
        'metar_dtdt': MetarDtdtSignal(db_path=METAR_DB),
    }

    report = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "axis": "strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction",
        "truth_source": "settlement_truth.strike_price (real strikes, NOT day-over-day)",
        "gate": "STRICT as-of: fetch_timestamp < target_date T00:00 (NOT fetch_date <= target_date)",
        "units": "mixed-units hygiene: °F-in-°C-field (> +50) normalized °F->°C before °C->°F signal conversion",
        "respec": "nwp_direct_signal strike-crossing (HIGH: temp_2m_max vs strike; LOW: temp_2m_min vs strike)",
        "strike_passed": "strike_price passed to nwp_direct + nwp_dtdt_fusion (correct axis); metar_dtdt scored without strike",
        "label_exclusions": excl,
    }

    scored = {}
    for name, sig in sigs.items():
        pass_strike = name in ('nwp_direct', 'nwp_dtdt_fusion')
        with contextlib.redirect_stderr(io.StringIO()):
            m = _score_one(sig, name, labels, meta, conn, pass_strike=pass_strike)
        if m['n'] == 0:
            m['status'] = 'NO_FIRES'
        else:
            m['status'] = 'PASS' if (m['mcc'] > 0 and m['base_rate_delta'] > 0) else 'NO_EDGE'
        scored[name] = m

    report["scores"] = scored
    report["elapsed_seconds"] = round(time.time() - t0, 2)
    conn.close()

    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = os.path.join(REPORTS_DIR, f"rr1c_strict_asof_rescore_{ts}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    print("=" * 100)
    print("R/R-1c — strict as-of re-score (strike-crossing axis, fetch_timestamp strict gate + units fix)")
    print("=" * 100)
    print(f"Axis: {report['axis']}")
    print(f"Gate: {report['gate']}")
    print(f"Base rate: up=34.36% (down-skewed); always-down null Sharpe ~5.25; MCC~0 = artifact")
    print()
    hdr = f"{'signal':<20s} {'MCC':>9s} {'Sharpe':>8s} {'baseΔ':>8s} {'acc':>8s} {'n':>6s} {'verdict':<12s}"
    print(hdr)
    print("-" * len(hdr))
    for name in ('nwp_direct', 'nwp_dtdt_fusion', 'metar_dtdt'):
        m = scored.get(name, {})
        if m.get('status') == 'NO_FIRES':
            print(f"{name:<20s} {'NO_FIRES':>9s}")
            continue
        sharpe_s = f"{m['sharpe']:.2f}" if m.get('sharpe') is not None else "n/a"
        print(f"{name:<20s} {m['mcc']:+9.4f} {sharpe_s:>8s} "
              f"{m['base_rate_delta']*100:+8.2f} {m['accuracy']*100:7.2f}% {m['n']:6d} {m.get('status','?'):<12s}")
    print()
    for name in ('nwp_direct', 'nwp_dtdt_fusion', 'metar_dtdt'):
        m = scored.get(name, {})
        if m and m.get('status') != 'NO_FIRES':
            print(f"  {name}: tp={m['tp']} fp={m['fp']} tn={m['tn']} fn={m['fn']}  "
                  f"up_ratio_pred={m['up_ratio_predicted']}")
    print()
    print(f"Label exclusions: {excl}")
    print(f"Receipt JSON: {path}")
    print(f"Elapsed: {report['elapsed_seconds']:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
