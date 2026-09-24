#!/usr/bin/env python3
"""
Observational-intraday as-of gate (R/R-1a — settlement-day lookahead fix, RE-DO).

Deterministic, single-source-of-truth predicate for excluding settlement-day
lookahead rows from the observational-intraday signal family (METAR observations
read directly at signal-evaluation time).

This is the observational analog of core/nwp_lookahead_gate.py, which protects
the NWP DB path with `fetch_date <= target_date`.  The NWP gate does NOT cover
signals that read raw METAR observations (`metar_observations` / `daily_stats`)
for the settlement day itself.

Semantics (CORRECTED — this is the R/R-1a Part 1 re-do)
----------------------------------------------------------
The ground-truth label a signal is scored against is the settlement day's
strike-crossing direction:

    settlement_temp_f  vs  strike_price   (HIGH: temp > strike; LOW: temp < strike)

`settlement_temp_f` is the settlement day's FULL-DAY extremum (the day's high for
HIGH markets, the day's low for LOW markets).  Therefore ANY observation with
`timestamp_utc >= settlement_day_start` ("the settlement window open") is
INFORMATION FROM THE FUTURE relative to the prediction — it already contains part
of the outcome the signal is trying to predict.

The as-of-safe predicate is therefore:

    timestamp_utc < settlement_window_open
        where settlement_window_open = settlement_date T00:00:00 UTC

BUT — and this is the defect fixed here — that predicate is ONLY meaningful when
the query's DATE SCOPE ALSO EXCLUDES the settlement day.  The prior (rejected)
implementation bolted `timestamp_utc < settlement_window_open` onto queries STILL
scoped `date_utc = settlement_date`.  That is a structural contradiction: it asks
for "settlement-day rows with timestamp before settlement-day-start," which is
EMPTY BY CONSTRUCTION (settlement-day rows all have `timestamp_utc >=
settlement_date T00:00:00`).  The resulting n=0 "collapse" was a NULLIFIED QUERY,
not a "no predictions" finding.

The CORRECT contemporaneous observation set for a same-day strike-crossing
prediction is the PRIOR day (and earlier) — i.e. rows with
`timestamp_utc < settlement_date T00:00:00`, which is equivalent to
`date_utc < settlement_date` (plus, optionally, a bounded prior-window lookback).

Do NOT gate on `date_utc = settlement_date` at all for the intraday observation
read — that day's observations are the outcome's own measurement stream and are
inherently lookahead for a same-day prediction.

Do NOT delete settlement-day rows from the DB.  They are legitimately needed by
explicitly-retrospective consumers (e.g. settlement verification, PnL accounting).
Gate at read time only.
"""

from datetime import datetime, timedelta

# The settlement day is the outcome day.  Its observation window opens at midnight
# (00:00:00 UTC) of the settlement date.  Anything at-or-after that timestamp is
# lookahead for a same-day strike-crossing prediction.
SETTLEMENT_WINDOW_OPEN_SUFFIX = "T00:00:00"


def settlement_window_open(settlement_date: str) -> str:
    """Return the ISO timestamp at which the settlement day's observation window opens.

    `settlement_date` is an ISO date 'YYYY-MM-DD' (the `local_trading_date` /
    `date_utc` of the outcome).  The window opens at that day's 00:00:00 UTC.
    """
    return f"{settlement_date}{SETTLEMENT_WINDOW_OPEN_SUFFIX}"


def prior_day(settlement_date: str) -> str:
    """Return the ISO date of the day immediately preceding `settlement_date`.

    This is the upper-bound (exclusive) date for the contemporaneous observation
    read: a same-day strike-crossing prediction may only use observations with
    `date_utc < settlement_date`, i.e. the prior day and earlier.
    """
    dt = datetime.strptime(settlement_date, "%Y-%m-%d")
    return (dt - timedelta(days=1)).strftime("%Y-%m-%d")


def lookback_start(settlement_date: str, lookback_days: int) -> str:
    """Return the inclusive lower-bound date for a bounded prior-window read.

    `lookback_days` = number of prior days to include.  Returns a date such that
    the window `[lookback_start, settlement_date)` admits `lookback_days` days of
    contemporaneous observations (e.g. lookback_days=1 -> just the prior day;
    lookback_days=7 -> the prior week).  Defaults to unbounded (None) when
    `lookback_days <= 0`.
    """
    if lookback_days is None or lookback_days <= 0:
        return None
    dt = datetime.strptime(settlement_date, "%Y-%m-%d")
    return (dt - timedelta(days=lookback_days)).strftime("%Y-%m-%d")


def is_asof_safe(timestamp_utc: str, settlement_date: str) -> bool:
    """True when an observation predates the settlement window (no lookahead).

    `timestamp_utc` is the observation's ISO timestamp; `settlement_date` is the
    outcome day 'YYYY-MM-DD'.  Lexicographic comparison is chronological for
    zero-padded ISO timestamps (the '+00:00' suffix sorts after the zero-padded
    digits, so `timestamp_utc < "dateT00:00:00"` correctly excludes the whole
    settlement day).
    """
    return timestamp_utc < settlement_window_open(settlement_date)


# SQL fragment for a CORRECT contemporaneous prior-day read.  Unlike the rejected
# predicate (` AND timestamp_utc < ?` bolted onto a `date_utc = ?` scope), this
# fragment scopes the DATE column to the prior day(s) and is intended to REPLACE
# the `date_utc = settlement_date` equality.  It binds a single parameter: the
# settlement date (the upper, exclusive bound for the date scope).
#
#   WHERE station = ? AND date_utc < ? AND timestamp_utc < ?   (with two binds:
#   settlement_date and settlement_window_open(settlement_date) — but since
#   date_utc < settlement_date already implies timestamp_utc < settlement_window_open,
#   the timestamp predicate is redundant here and kept only as belt-and-suspenders).
PRIOR_DAY_PREDICATE = " AND date_utc < ?"


def asof_where(clause: str) -> str:
    """Return `clause` with the CORRECTED prior-day as-of predicate appended.

    Pass an existing WHERE fragment (without the leading 'WHERE').  The result is
    `clause` + ` AND date_utc < ?`.  The caller MUST append the settlement date
    as the final bound parameter — this REPLACES any prior `date_utc = ?` equality
    (which was the source of the zero-by-contradiction bug).
    """
    return clause + PRIOR_DAY_PREDICATE
