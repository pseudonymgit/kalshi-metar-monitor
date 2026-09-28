#!/usr/bin/env python3
"""
GS-B2 — Target-invariance test for every registered signal.

Contract: a signal's output at index `idx` must depend ONLY on days[0..idx-1]
(history available before the target) plus the target's *metadata* (station, date)
— NEVER on the target day's observed values (days[idx]['high'/'low'/'temp'/...).

Proof: evaluate each signal at `idx` on a base series, then re-evaluate with the
target day's observation fields perturbed by +500 / -500. Outputs must be
byte-identical. Any signal whose output changes is LEAKING (reads the answer).

Run: .venv-gefs/bin/python tests/test_target_invariance.py
Exit non-zero if any signal leaks (usable as a blocking deploy gate).
"""
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

from signals import SignalRegistry  # noqa: E402

# Observation fields that constitute "the answer" for the target day.
TARGET_FIELDS = ["high", "low", "temp", "dewpoint", "pressure", "wind_speed",
                 "wind_direction", "cloud_cover", "humidity", "visibility"]

BASE_DAY = {
    "high": 70.0, "low": 55.0, "temp": 70.0, "dewpoint": 55.0,
    "pressure": 1013.0, "wind_speed": 5.0, "wind_direction": 180.0,
    "cloud_cover": 30.0, "humidity": 60.0, "visibility": 10.0,
}


def make_series(n=40, station="KNYC"):
    from datetime import date, timedelta
    start = date(2026, 5, 1)
    days = []
    for i in range(n):
        d = dict(BASE_DAY)
        d.update({"date": (start + timedelta(days=i)).isoformat(), "station": station,
                  "high": 70.0 + ((i * 7) % 5), "temp": 70.0 + ((i * 3) % 4),
                  "low": 55.0 + (i % 3), "pressure": 1013.0 + (i % 3)})
        days.append(d)
    return days


def evaluate(signal, days, idx):
    try:
        return signal.evaluate(idx, days)
    except TypeError:
        # some signatures differ
        try:
            return signal.evaluate(idx, days, "HIGH")
        except Exception as e:
            return ("__ERROR__", str(e))


def main():
    reg = SignalRegistry(str(ROOT / "data" / "metar_backfill.db"))
    signals = reg.get_all_signals()
    base = make_series()
    idx = len(base) - 1

    leaks = []
    checked = 0
    for name, sig in sorted(signals.items()):
        d1 = copy.deepcopy(base)
        d2 = copy.deepcopy(base)
        d3 = copy.deepcopy(base)
        for f in TARGET_FIELDS:
            if f in d2[idx]:
                d2[idx][f] = d2[idx][f] + 500.0
                d3[idx][f] = d3[idx][f] - 500.0
        try:
            r1 = evaluate(sig, d1, idx)
            r2 = evaluate(sig, d2, idx)
            r3 = evaluate(sig, d3, idx)
        except Exception as e:
            leaks.append((name, f"EXCEPTION {e}"))
            continue
        checked += 1
        if not (r1 == r2 == r3):
            leaks.append((name, f"{r1} vs {r2} vs {r3}"))

    print(f"TARGET-INVARIANCE: checked {checked} signals")
    if leaks:
        print(f"🚨 LEAKS DETECTED ({len(leaks)}):")
        for n, detail in leaks:
            print(f"   - {n}: {detail}")
        return 1
    print("✅ All signals target-invariant")
    return 0


if __name__ == "__main__":
    sys.exit(main())
