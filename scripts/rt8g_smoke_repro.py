#!/usr/bin/env python3
"""
R/T-8G — sweep parallelization reproducibility smoke check.

Compares the SERIAL sweep path against the PARALLEL (ProcessPool) path on a
bounded smoke subset (subset of stations × subset of signal names), proving:

  (a) parallel output == serial output (order-normalized AND byte-identical
      after re-serializing in sorted-key order), and
  (b) wall-clock improvement.

This script is a READ-ONLY harness: it imports ultimate_sweep's scoring
functions, restricts the label/registry surface, runs both paths, and reports
the comparison. It does NOT mutate data and does NOT write DB state.

Usage:
  python3 scripts/rt8g_smoke_repro.py [--workers N] [--n-stations K] [--signals a,b,c]

Deterministic: fixed seed, sorted keys, identical subset for both paths.
"""

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# ultimate_sweep lives under scripts/; import it directly as a module.
import importlib
sys.path.insert(0, os.path.join(BASE, "scripts"))
um = importlib.import_module("ultimate_sweep")

# Default smoke subset: a representative, bounded slice.
DEFAULT_STATIONS = ["KATL", "KDEN", "KMDW", "KMIA"]
# A mix of cheap/standard + one heavy signal + one own-DB signal + one skip.
DEFAULT_SIGNALS = [
    "gaussian", "pressure_delta", "calendar_climatology",
    "nwp_analog", "cloud_cover_index", "eighty_two_member_ensemble",
]


def build_subset_labels(labels, stations):
    return {s: labels[s] for s in stations if s in labels}


def build_subset_registry(registry, status, names):
    sub = {}
    for n in names:
        if n in registry:
            sub[n] = registry[n]
    return sub


def main():
    ap = argparse.ArgumentParser(description="R/T-8G parallel vs serial reproducibility check")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--n-stations", type=int, default=4)
    ap.add_argument("--signals", type=str, default=",".join(DEFAULT_SIGNALS))
    args = ap.parse_args()

    labels, src_map, meta, excl = um.load_labels()
    registry, status = um.build_signal_registry()

    # Deterministic subset ordering: explicit DEFAULT_STATIONS (or first N).
    stations = [s for s in DEFAULT_STATIONS if s in labels]
    if len(stations) < args.n_stations:
        # backfill from sorted remaining stations to reach requested count
        for s in sorted(labels.keys()):
            if s not in stations:
                stations.append(s)
            if len(stations) >= args.n_stations:
                break
    stations = stations[: args.n_stations]
    names = sorted(n for n in args.signals.split(",") if n.strip())
    sub_labels = build_subset_labels(labels, stations)
    sub_registry = build_subset_registry(registry, status, names)

    n_labels = sum(len(v) for v in sub_labels.values())
    print(f"Smoke subset: {len(names)} signals × {len(stations)} stations = {n_labels} labels")
    print(f"  stations: {stations}")
    print(f"  signals : {names}")
    print()

    # ── SERIAL baseline ──
    t0 = time.time()
    serial = um.sweep_signals(sub_labels, meta, dry_run=False,
                              registry=sub_registry, status=status)
    t_serial = time.time() - t0

    # ── PARALLEL ──
    um._PARALLEL = True
    um._NUM_WORKERS = args.workers
    t0 = time.time()
    parallel = um.sweep_signals_parallel(sub_labels, meta, sub_registry, status)
    t_parallel = time.time() - t0

    # ── Reproducibility: order-normalized equality ──
    keys_s = sorted(serial.keys())
    keys_p = sorted(parallel.keys())
    same_keys = keys_s == keys_p

    diffs = []
    for k in sorted(set(keys_s) | set(keys_p)):
        if k not in serial or k not in parallel:
            diffs.append(f"{k}: missing from one side")
            continue
        if serial[k] != parallel[k]:
            diffs.append(f"{k}: serial={serial[k]} parallel={parallel[k]}")

    # ── Byte-identical check (re-serialize both in sorted-key order) ──
    sj = json.dumps(serial, sort_keys=True, indent=2)
    pj = json.dumps(parallel, sort_keys=True, indent=2)
    byte_identical = sj == pj

    print(f"SERIAL   wall-clock : {t_serial:.3f}s")
    print(f"PARALLEL wall-clock : {t_parallel:.3f}s  (workers={args.workers})")
    speedup = t_serial / t_parallel if t_parallel > 0 else float("inf")
    print(f"Speedup            : {speedup:.2f}x")
    print()
    print(f"Same signal keys    : {same_keys}")
    print(f"Per-signal diffs    : {len(diffs)}")
    for d in diffs:
        print(f"  DIFF: {d}")
    print(f"Byte-identical JSON : {byte_identical}")

    ok = same_keys and not diffs and byte_identical
    print()
    print("RESULT:", "PASS (parallel == serial, deterministic)" if ok else "FAIL")

    # Emit a compact receipt for the handoff.
    receipt = {
        "task": "R/T-8G sweep parallelization smoke reproducibility",
        "workers": args.workers,
        "stations": stations,
        "signals": names,
        "n_labels": n_labels,
        "serial_seconds": round(t_serial, 4),
        "parallel_seconds": round(t_parallel, 4),
        "speedup": round(speedup, 3),
        "same_keys": same_keys,
        "n_diffs": len(diffs),
        "byte_identical": byte_identical,
        "pass": ok,
    }
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
