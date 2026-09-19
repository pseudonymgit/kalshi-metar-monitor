#!/usr/bin/env python3
"""
NWP lookahead gate (P0-F).

Deterministic, single-source-of-truth predicate for excluding lookahead rows
from `data/nwp_forecasts.db`.

Semantics (from scripts/nwp_collect.py):
  - `fetch_date`  = the UTC date the forecast was fetched (the "as-of" date).
  - `target_date` = the date the forecast is FOR (the prediction horizon).
  - A row is CONTEMPORANEOUS (no lookahead) when `fetch_date <= target_date`.
  - A row is LOOKAHEAD (reanalysis/backfill masquerading as a forecast) when
    `fetch_date > target_date`.

Leak found 2026-09-19: 317,669 rows (34.1%) had `fetch_date > target_date`,
dominated by `gefs_ens` ensemble members (245,029) and `ecmwf` upper-air
backfill (71,440).  The upper-air fields
(`geopotential_height_500hPa_daily_mean`, `temperature_850hPa_daily_mean`)
that power `nwp_analog` had 37,260 contaminated (station,target_date) keys
vs only 2,800 contemporaneous — 34,460 keys existed ONLY via lookahead.

Every NWP-dependent signal MUST filter on this predicate.  Do not remove
lookahead rows from the DB (they may be reanalysis legitimately needed by
other, explicitly-retrospective consumers); instead gate at read time.
"""

# SQL fragment to append to any nwp_forecasts WHERE clause.  Safe to
# concatenate after existing conditions (leading "AND").
CONTEMPORANEOUS_PREDICATE = " AND fetch_date <= target_date"


def contemporaneous_where(clause: str) -> str:
    """Return `clause` with the contemporaneous predicate appended.

    Pass an existing WHERE fragment (without the leading 'WHERE').  The result
    is `clause` + ` AND fetch_date <= target_date`.
    """
    return clause + CONTEMPORANEOUS_PREDICATE


def is_lookahead(fetch_date: str, target_date: str) -> bool:
    """True when a row is lookahead (fetch_date strictly after target_date).

    Dates are ISO 'YYYY-MM-DD'; lexicographic comparison == chronological
    comparison for zero-padded ISO dates.
    """
    return fetch_date > target_date
