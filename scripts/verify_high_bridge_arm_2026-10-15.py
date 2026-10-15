#!/usr/bin/env python3
"""verify_high_bridge_arm_2026-10-15.py — verify the HIGH SBOX bridge is armed
correctly on CURRENT code, and dry-run it, for the fresh HIGH book.

Checks (all read-only; the dry-run writes nothing):
  1. `USE_OM_GFS_NWP=1` in .env  -> om_gfs is the SOLE forecast
     (bridge.sole_forecast_enabled() must be True).
  2. Kelly sizing is the active path (bridge._position_size_usd is
     bridge.kelly_position_size, i.e. not the retired confidence ladder).
  3. settle writer is contract-notional (core/pnl_tracking.py scores
     (payout - price) * quantity) — verify the source contains the qty form.
  4. `LOW_BRIDGE_ENABLED` is OFF/unset in .env.
  5. The book write path is reachable (SBOX_DB exists, trades table present).
  6. Dry-run the bridge (--dry-run --ignore-window): computes forecasts, finds
     strikes, reports would-book count WITHOUT writing.

usage: .venv-gefs/bin/python scripts/verify_high_bridge_arm_2026-10-15.py
"""
from __future__ import annotations

import importlib.util as ilu
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(REPO / "scripts"))
OUT = REPO / "artifacts" / "high-bridge-arm-2026-10-15.json"


def env_flag(name: str):
    """Read a flag from .env (last assignment wins, both `export X=` and `X=`)."""
    val = None
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            m = re.match(rf"^{re.escape(name)}\s*=\s*(.*)$", line)
            if m:
                val = m.group(1).strip().strip('"').strip("'")
    return val


def main() -> int:
    art: dict = {"generated_by": str(Path(__file__).name)}

    # ---- 1/4: env flags ------------------------------------------------------
    use_om = env_flag("USE_OM_GFS_NWP")
    low = env_flag("LOW_BRIDGE_ENABLED")
    art["env"] = {"USE_OM_GFS_NWP": use_om, "LOW_BRIDGE_ENABLED": low}

    # ---- import the bridge (this loads .env itself) -------------------------
    spec = ilu.spec_from_file_location(
        "bridge", REPO / "scripts" / "sbox_intraday_ev_bridge.py")
    b = ilu.module_from_spec(spec)
    spec.loader.exec_module(b)

    art["sole_forecast_enabled"] = bool(b.sole_forecast_enabled())
    art["position_size_fn"] = getattr(b._position_size_usd, "__name__", "?")
    # `_position_size_usd` is the bridge's sizing entry point; it must delegate
    # to the Kelly implementation (not the retired confidence ladder). Behavioral
    # check: same inputs -> same output as kelly_position_size, and a no-edge
    # input yields $0 (the guard the ladder lacked).
    sample = [(0.60, 0.40), (0.30, 0.50), (0.50, 0.50)]
    art["kelly_is_active_path"] = all(
        abs(b._position_size_usd(p, m) - b.kelly_position_size(p, m)) < 1e-9
        for p, m in sample)
    art["kelly_zero_edge_returns_zero"] = (b._position_size_usd(0.40, 0.45) == 0.0)
    art["booking_gate_enabled"] = bool(getattr(b, "ENABLE_BOOKING_GATE", False))
    art["sbox_db"] = str(Path(b.SBOX_DB).relative_to(REPO))

    # ---- 3: settle writer contract-notional ---------------------------------
    pnl_src = (REPO / "core" / "pnl_tracking.py").read_text()
    art["settle_writer_contract_notional"] = bool(
        re.search(r"\(settlement_contract_value\s*-\s*filled_price\)\s*\*\s*qty", pnl_src))

    # ---- 5: book write path reachable ---------------------------------------
    db = Path(b.SBOX_DB)
    book = {"exists": db.exists()}
    if db.exists():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            book["trades_rows"] = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
            book["has_quantity"] = "quantity" in cols
            book["has_position_size_usd"] = "position_size_usd" in cols
            book["has_lane"] = "lane" in cols
        finally:
            c.close()
    art["book"] = book

    # ---- 6: dry-run (writes nothing) ----------------------------------------
    dry = b.run(dry_run=True, ignore_window=True)
    art["dry_run"] = {
        "date_utc": dry.get("date_utc"),
        "n_executed": dry.get("n_executed"),
        "n_skipped": dry.get("n_skipped"),
        "skip_reasons": {},
        "would_execute": [
            {"station": e["station"], "side": e["side"], "strike": e["strike"],
             "our_prob": round(e["our_prob"], 4),
             "market_price": e["market_price"], "size": e["size"]}
            for e in dry.get("executed", [])],
    }
    from collections import Counter
    art["dry_run"]["skip_reasons"] = dict(
        Counter(s[1] for s in dry.get("skipped", [])))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(art, indent=2))

    verdict = (
        art["sole_forecast_enabled"]
        and art["kelly_is_active_path"]
        and art["kelly_zero_edge_returns_zero"]
        and art["settle_writer_contract_notional"]
        and low in (None, "0", "false", "False")
        and art["booking_gate_enabled"]
    )
    art["ARMED"] = bool(verdict)
    OUT.write_text(json.dumps(art, indent=2))
    print(json.dumps(art, indent=2))
    print(f"\n[arm] ARMED={verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
