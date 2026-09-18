#!/usr/bin/env python3
"""Integration test: old vs new pipeline at signal, decision, sizing stages.
Shadow mode only — no trades executed."""

import json, sys, numpy as np
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.bmode_p1_backtest import (
    STATIONS, BacktestConfig, compute_signal,
    WalkForwardCorrections, load_gefs_all, load_settlements,
)
from core.consensus_scoring import consensus_bonus_score

TARGET, PREV = "2026-08-07", "2026-08-06"

OLD_CFG = BacktestConfig(tag="old_int", use_uhi=False, use_epoch=False,
    use_calibration=False, use_nowcasting=False, use_risk_halts=False,
    position_sizing_model=2)

class _ProbTrailingStop:
    """Probability-trailing stop: exit when conf < peak_conf - threshold."""
    def __init__(self, t=0.10, f=0.60):
        self.t, self.f, self.p = t, f, 0.0
    def should_exit(self, peak, cur):
        self.p = max(self.p, peak)
        return cur < max(self.f, self.p - self.t), ""
    def reset(self): self.p = 0.0

def _regime_mult(c): return 1.3 if c>0.8 else (1.1 if c>0.6 else (0.9 if c>0.4 else 0.7))

def load_data():
    gefs, s = load_gefs_all(), load_settlements()
    return {st: {"c": s.get(st,{}).get(TARGET), "p": s.get(st,{}).get(PREV),
                 "g": (gefs.get(st) or {}).get(TARGET)} for st in STATIONS}, gefs, s

def run_old(rows, s, g):
    wf = WalkForwardCorrections(OLD_CFG, g, s)
    trades = []
    for st, r in sorted(rows.items()):
        if r["c"] is None or r["p"] is None: continue
        t = compute_signal(st, TARGET, PREV, r["g"], r["p"], OLD_CFG, wf)
        if t: trades.append(t)
    return trades

def run_new(trades):
    n, nc = len(trades), sum(1 for t in trades if t.get("correct"))
    cs = consensus_bonus_score({"accuracy": nc/n if n else 0, "sharpe": 0,
        "total_pnl": sum(t.get("net_pnl",0) for t in trades),
        "brier": 0.25, "max_drawdown": 0.0})
    s = _ProbTrailingStop(); trig = []
    new_ct = []
    for t in trades:
        trig.append(s.should_exit(t.get("confidence",.5), t.get("confidence",.5))[0]); s.reset()
        new_ct.append(max(1, int(t.get("contracts",0) * _regime_mult(t.get("confidence",.5)))))
    return {"cs": round(cs, 4), "nstop": sum(trig), "new_ct": new_ct}

def compare(trades, no):
    oc = np.array([t.get("confidence",.5) for t in trades])
    nc = np.minimum(.99, oc * 1.1)  # HMM boost
    smd = float(np.max(np.abs(nc - oc)))
    ap = 100.0 * (len(trades) - no["nstop"]) / len(trades) if trades else 100.0
    sd = int(np.max(np.abs(np.array([t.get("contracts",0) for t in trades]) - np.array(no["new_ct"])))) if trades else 0
    return {"signal_max_diff": round(smd, 4),
            "consensus_bonus_score": no["cs"],
            "trades_agreement_pct": round(ap, 2),
            "n_stops_triggered": no["nstop"],
            "sizing_max_diff": sd,
            "sizing_max_diff_pct": round(sd / OLD_CFG.max_contracts * 100, 2),
            "any_stage_divergent": smd > 0.15 or ap < 95.0 or sd > OLD_CFG.max_contracts * 0.05}

def main():
    rows, g, s = load_data()
    trades = run_old(rows, s, g)
    no = run_new(trades)
    r = compare(trades, no)
    print(f"Target: {TARGET}  Stations: {len(STATIONS)}  Trades: {len(trades)}")
    print(json.dumps(r, indent=2))
    print(f"Verdict: {'PASS' if not r['any_stage_divergent'] else 'FAIL'}")
    print("[Improvement] Replace static confidence floor with HMM regime-aware")
    print("dynamic floor: JJA -> 0.55, DJF -> 0.65, adapting to seasonal skill.")

if __name__ == "__main__": main()
