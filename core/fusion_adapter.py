#!/usr/bin/env python3
"""
core/fusion_adapter.py — PARKED STUB (intentionally empty).

C2 (GR3 D4): this adapter is 0 bytes. The live backtest path does NOT use the
LLOP/UWC fusion chain — `core/unified_backtest.py` runs a majority vote with
`use_fusion=False` (hard-asserted). Fusion output is not decision-grade until
the blocking prerequisites close:

  1. axis fix (A4)                        — landed (strict strike-crossing)
  2. n_eff / rho correction (C3 / D6)     — dead config field today
  3. LLOP weight positional alignment (C3 / D5) — else sum(w) != 1, double-count
  4. confidence promoted to a probability (D7)

Do NOT wire fusion here until all four hold. See
docs/plans/GRAY-ROOM-3-SYNTHESIS-2026-10-09.md (D4) and the C2/C3 notes in
core/unified_backtest.py.
"""
# No implementation. Fusion is PARKED (GR3 D4).
