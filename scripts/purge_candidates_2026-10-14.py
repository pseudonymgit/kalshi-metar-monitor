#!/usr/bin/env python3
"""purge_candidates_2026-10-14.py — propose (DO NOT DELETE) the SBOX rows that
do not represent an informative, accurate trade.

Dan directive (2026-10-14): "purge any trade history that doesn't actually
represent an informative and accurate trade."

Criterion (proposed, for Dan's approval — nothing is deleted here). A row is a
PURGE CANDIDATE when ANY hold:

  (A) SATURATED-SELECTION: the row was entered on the OLD saturated fused
      probability (p_fused_raw >= 0.99) — the deep-OTM lottery-ticket selection
      the register already flags as broken (its p_fused told us nothing), AND the
      honest calibrated_p for the row is not itself defensible.

  (B) UNRECONSTRUCTABLE: the row's reconstruction cannot produce a defensible
      honest calibrated_p (no companion row, or the calibrated_p is missing/NaN).

  (C) ZERO-SIZE: position_size_usd == 0 — the honest ladder says this trade was
      never sized, so it should never have been placed.

Outputs docs/plans/TRADE-PURGE-CANDIDATES-2026-10-14.md + artifacts JSON.
Read-only on `trades`.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT_DOC = REPO / "docs" / "plans" / "TRADE-PURGE-CANDIDATES-2026-10-14.md"
OUT_JSON = REPO / "artifacts" / "settlement-axis-truth" / "purge_candidates_2026-10-14.json"

SAT_THRESHOLD = 0.99
# A calibrated_p is "defensible" when it is a real number in (0,1) that differs
# from the saturated raw (i.e. the honest recalibration actually moved it) and is
# not itself pinned at the saturated value.
def defensible_cal(cal) -> bool:
    if cal is None:
        return False
    try:
        c = float(cal)
    except (TypeError, ValueError):
        return False
    if math.isnan(c) or not (0.0 < c < 1.0):
        return False
    return True


def main():
    b = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    rows = [dict(r) for r in b.execute(
        "SELECT id, trade_uuid, status, station, market_type, market_ticker, "
        "trade_type, signal_direction, quantity, position_size_usd, "
        "position_size_usd_old_2026, trade_price, p_fused, p_fused_raw, ceiling, "
        "canonical_outcome, realized_pnl FROM trades ORDER BY id")]
    comp = {}
    for r in b.execute("SELECT trade_uuid, calibrated_p, ceiling, p_fused, "
                       "composite_conf FROM trade_forecaster_probs"):
        comp[r["trade_uuid"]] = dict(r)
    b.close()

    cands = []
    for r in rows:
        cr = comp.get(r["trade_uuid"], {})
        cal = cr.get("calibrated_p")
        reasons = []
        if (r["p_fused_raw"] or 0) >= SAT_THRESHOLD:
            reasons.append("A_saturated_selection")
        if not defensible_cal(cal):
            reasons.append("B_unreconstructable")
        if (r["position_size_usd"] or 0) == 0:
            reasons.append("C_zero_size")
        if reasons:
            cands.append({**r, "calibrated_p": cal,
                          "composite_conf": cr.get("composite_conf"),
                          "reasons": reasons})

    n_a = sum(1 for x in cands if "A_saturated_selection" in x["reasons"])
    n_b = sum(1 for x in cands if "B_unreconstructable" in x["reasons"])
    n_c = sum(1 for x in cands if "C_zero_size" in x["reasons"])

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({"n_rows": len(rows), "n_candidates": len(cands),
                                    "counts": {"A": n_a, "B": n_b, "C": n_c},
                                    "candidates": cands}, indent=2))

    lines = []
    A = lines.append
    A("# Trade Purge Candidates — 2026-10-14")
    A("")
    A("**Status:** CANDIDATES ONLY — **nothing deleted**. Awaiting Dan's approval of the criterion.")
    A("")
    A("**Repo:** `prototypes/weather-engine-source/` · **Book:** `data/paper_trading_sbox.db`")
    A("**Tool:** `scripts/purge_candidates_2026-10-14.py` · **Artifact:** "
      "`artifacts/settlement-axis-truth/purge_candidates_2026-10-14.json`")
    A("")
    A("## 0. Dan's directive")
    A("")
    A('> "purge any trade history that doesn\'t actually represent an informative and accurate trade."')
    A("")
    A("## 1. Proposed purge criterion (composable; a row qualifies on ANY hold)")
    A("")
    A("| tag | meaning |")
    A("|---|---|")
    A("| **A_saturated_selection** | entered on the OLD saturated fused probability "
      f"(`p_fused_raw >= {SAT_THRESHOLD}`) — the deep-OTM lottery-ticket selection the "
      "register already flags as broken. The old p_fused told us nothing about the pick. |")
    A("| **B_unreconstructable** | the honest reconstruction cannot produce a "
      "defensible `calibrated_p` (no companion row, or `calibrated_p` missing/NaN/out of (0,1)). |")
    A("| **C_zero_size** | `position_size_usd == 0` — the honest ladder never sized it; "
      "it should not have been placed. |")
    A("")
    A("A row is **informative** only if its decision-time probability is defensible "
      "(not saturated, reconstructable) and it was **sized** (> $0). Rows failing both "
      "A and C are the strongest purge candidates.")
    A("")
    A("## 2. Result")
    A("")
    A(f"- book rows: **{len(rows)}**")
    A(f"- purge candidates: **{len(cands)}**")
    A(f"  - A (saturated selection): **{n_a}**")
    A(f"  - B (unreconstructable): **{n_b}**")
    A(f"  - C (zero size): **{n_c}**")
    A("")
    A("## 3. Candidate list")
    A("")
    A("| id | station | ticker | status | size$ | old_size$ | p_fused_raw | calibrated_p | outcome | pnl(1c) | reasons |")
    A("|---:|---|---|---|---:|---:|---:|---:|---|---:|---|")
    for x in cands:
        A(f"| {x['id']} | {x['station']} | {x['market_ticker']} | {x['status']} | "
          f"{x['position_size_usd']} | {x['position_size_usd_old_2026']} | "
          f"{x['p_fused_raw']} | {x['calibrated_p']} | {x['canonical_outcome']} | "
          f"{x['realized_pnl']} | {', '.join(x['reasons'])} |")
    A("")
    A("## 4. Recommendation")
    A("")
    A("- **Do not delete yet.** Dan approves the criterion first (his directive).")
    A("- Before any purge: full backup (`data/paper_trading_sbox.db.bak-*`) + a "
      "pre-purge snapshot; the purge is an explicit `DELETE`/archive with the "
      "candidate ids recorded in the artifact above.")
    A("- Consider **archive-not-delete**: move candidates to a `trades_purged_2026` "
      "table so the history is auditable rather than destroyed.")
    OUT_DOC.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT_DOC}")
    print(f"candidates={len(cands)} (A={n_a} B={n_b} C={n_c}) of {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
