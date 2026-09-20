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

AS-OF KEY CORRECTION (R/R-1c, 2026-09-20 — Gerri-verified data-level defect):
  `fetch_date` is an UNTRUSTWORTHY as-of key.  Of 63,665 `temperature_2m_max`
  rows, 29,900 (47%) have `substr(fetch_timestamp,1,10) != fetch_date`.  The
  two backfill scripts wrote contradictory values into `fetch_date`:
    * scripts/nwp_backfill_max.py  -> `fetch_date = target_date` (same-day)
    * scripts/nwp_backfill_30d.py -> `fetch_date = daily.time[0]` (first
      horizon date)
  Both make historical rows *look* contemporaneous when the forecast was in
  fact fetched (backfilled) months later.  `fetch_timestamp` is the wall-clock
  time the row was actually written, so it is the correct as-of key: a row is
  trustworthy as-of data for target day `T` only when it was fetched strictly
  BEFORE `T` began (`fetch_timestamp < T T00:00:00`).

  Strict gate survival (temperature_2m_max): 28,390 / 63,665 rows (44.6%),
  target_date range 2026-07-23 .. 2026-09-26 (the live-collected window).  The
  historical backfill (2022-12 .. 2026-07-22) is excluded because it was all
  fetched in a July-2026 batch, after its own target dates.

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


# ── STRICT AS-OF GATE (R/R-1c) ──────────────────────────────────────────────
#
# The contemporaneous predicate above (`fetch_date <= target_date`) is INSECURE
# for same-day strike-crossing predictions: it admits same-day late nowcasts
# (rows fetched on the target day, after the day's high has already occurred).
#
# The strict predicate gates on `fetch_timestamp` (the true wall-clock as-of
# time), requiring the forecast to have been fetched STRICTLY BEFORE the target
# day began.


def strict_asof_predicate() -> str:
    """SQL fragment (no leading AND) for the strict as-of gate.

    Requires `fetch_timestamp < ?` where `?` is bound to `"<target_date>T00:00:00"`.
    `fetch_timestamp` is an ISO-8601 string with a `+00:00` UTC suffix, so
    lexicographic comparison against the target-day-midnight bound sorts the
    entire target day AFTER the bound — i.e. only strictly-prior-day fetches
    survive.
    """
    return "fetch_timestamp < ?"


def strict_asof_bound(target_date: str) -> str:
    """Return the parameter bound for the strict gate: `<target_date>T00:00:00`."""
    return f"{target_date}T00:00:00"
