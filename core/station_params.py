#!/usr/bin/env python3
"""
Per-Station Entry/Position Parameters (F4 — 2026-10-13)
=======================================================
Every station previously shared one entry window, one ladder, and one edge
floor. This module owns a `station_params` table (a NEW table — the live book is
never written) that lets the SBOX bridge look those values up per station.

Schema:
    station_params(
        station TEXT PRIMARY KEY,
        entry_window_start TEXT,   -- earliest entry, as "T-<h>h" before settlement
        entry_window_end   TEXT,   -- latest entry, as "T-<h>h" before settlement
        position_cap_usd   REAL,
        confidence_floor   REAL,
        regime             TEXT    -- provenance: marine|convective|continental
    )

Heterogeneity is intentional: marine/coastal regimes (long, stable diurnal
curves) may enter earlier (a longer window); convective regimes (afternoon
storm blow-ups) enter later. Defaults preserve the historical 18h..2h window,
$25 cap, and no extra confidence floor; only the window is differentiated.

Deterministic. Read-only on every book table.
"""

import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, Optional

_REPO = Path(__file__).resolve().parents[1]
STATION_PARAMS_DB = _REPO / "data" / "station_params.db"

# Regime defaults: (window_start_hb, window_end_hb, position_cap_usd, confidence_floor)
# window_start_hb = hours before settlement at which entry opens (larger = earlier).
# window_end_hb   = hours before settlement at which entry closes.
_REGIME_DEFAULTS = {
    "continental": ("T-18h", "T-2h", 25.0, 0.0),
    "marine":      ("T-20h", "T-2h", 25.0, 0.0),   # longer window
    "convective":  ("T-14h", "T-2h", 25.0, 0.0),   # shorter window
}

# All 20 SBOX stations -> regime. Marine = coastal/stable; convective = Plains/
# Gulf afternoon-storm regimes; continental = the remainder.
STATION_REGIME: Dict[str, str] = {
    "KATL": "convective",   "KAUS": "convective",  "KBOS": "marine",
    "KDCA": "continental",  "KDEN": "convective",  "KDFW": "convective",
    "KHOU": "convective",   "KLAS": "continental", "KLAX": "marine",
    "KMDW": "continental",  "KMIA": "marine",      "KMSP": "continental",
    "KMSY": "marine",       "KNYC": "continental", "KOKC": "convective",
    "KPHL": "continental",  "KPHX": "continental", "KSAT": "convective",
    "KSEA": "marine",       "KSFO": "marine",
}


def parse_window_token(token: str) -> float:
    """'T-18h' -> 18.0 (hours before settlement). Raises on a malformed token."""
    m = re.fullmatch(r"T-(\d+(?:\.\d+)?)h", (token or "").strip())
    if not m:
        raise ValueError(f"malformed window token: {token!r}")
    return float(m.group(1))


def default_params_for(station: str) -> Dict:
    regime = STATION_REGIME.get((station or "").upper(), "continental")
    start, end, cap, floor = _REGIME_DEFAULTS[regime]
    return {
        "station": (station or "").upper(),
        "entry_window_start": start,
        "entry_window_end": end,
        "position_cap_usd": cap,
        "confidence_floor": floor,
        "regime": regime,
    }


def ensure_station_params(conn: sqlite3.Connection) -> None:
    """Create + seed the station_params table (idempotent). Existing rows kept."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS station_params (
            station TEXT PRIMARY KEY,
            entry_window_start TEXT NOT NULL,
            entry_window_end TEXT NOT NULL,
            position_cap_usd REAL NOT NULL,
            confidence_floor REAL NOT NULL,
            regime TEXT
        )
    """)
    for st in STATION_REGIME:
        d = default_params_for(st)
        conn.execute(
            "INSERT OR IGNORE INTO station_params "
            "(station, entry_window_start, entry_window_end, position_cap_usd, "
            " confidence_floor, regime) VALUES (?,?,?,?,?,?)",
            (d["station"], d["entry_window_start"], d["entry_window_end"],
             d["position_cap_usd"], d["confidence_floor"], d["regime"]))
    conn.commit()


def _connect(db_path: Optional[Path] = None):
    path = Path(db_path) if db_path else STATION_PARAMS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    ensure_station_params(conn)
    return conn


def get_station_params(station: str, db_path: Optional[Path] = None) -> Dict:
    """Per-station params with parsed window bounds; falls back to regime defaults.

    Returns keys: station, entry_window_start, entry_window_end,
    entry_window_start_hb, entry_window_end_hb, position_cap_usd,
    confidence_floor, regime.
    """
    st = (station or "").upper()
    row = None
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT station, entry_window_start, entry_window_end, "
                "position_cap_usd, confidence_floor, regime FROM station_params "
                "WHERE station=?", (st,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    if row is None:
        d = default_params_for(st)
        return {**d,
                "entry_window_start_hb": parse_window_token(d["entry_window_start"]),
                "entry_window_end_hb": parse_window_token(d["entry_window_end"])}
    station, start, end, cap, floor, regime = row
    return {
        "station": station, "entry_window_start": start, "entry_window_end": end,
        "entry_window_start_hb": parse_window_token(start),
        "entry_window_end_hb": parse_window_token(end),
        "position_cap_usd": float(cap), "confidence_floor": float(floor),
        "regime": regime,
    }
