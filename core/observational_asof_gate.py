#!/usr/bin/env python3
"""
Observational-intraday as-of gate (R/R-1a — settlement-day lookahead fix).

Deterministic, single-source-of-truth predicate for excluding settlement-day
lookahead rows from the observational-intraday signal family (METAR observations
read directly at signal-evaluation time).

This is the observational analog of core/nwp_lookahead_gate.py, which protects
the NWP DB path with `fetch_date <= target_date`.  The NWP gate does NOT cover
signals that read raw METAR observations (`metar_observations` / `daily_stats`)
for the settlement day itself.

Semantics
---------
The ground-truth label a signal is scored against is the settlement day's
strike-crossing direction:

    settlement_temp_f  vs  strike_price   (HIGH: temp > strike; LOW: temp < strike)

`settlement_temp_f` is the settlement day's FULL-DAY extremum (the day's high for
HIGH markets, the day's low for LOW markets).  Therefore any observation with
`timestamp_utc >= settlement_day_start` ("the settlement window open") is
INFORMATION FROM THE FUTURE relative to the prediction — it already contains part
of the outcome the signal is trying to predict.

The as-of-safe predicate is:

    timestamp_utc < settlement_window_open

where `settlement_window_open` = the settlement day's start (`<date>T00:00:00`).
Strictly-before, analogous to `fetch_date <= target_date` (which admits a forecast
fetched ON the target date because the forecast is for a *future* horizon; here an
observation made DURING the settlement day is NOT a forecast, it is the outcome's
own measurement stream).

Do NOT delete settlement-day rows from the DB.  They are legitimately needed by
explicitly-retrospective consumers (e.g. settlement verification, PnL accounting).
Gate at read time only.
"""

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


def is_asof_safe(timestamp_utc: str, settlement_date: str) -> bool:
    """True when an observation predates the settlement window (no lookahead).

    `timestamp_utc` is the observation's ISO timestamp; `settlement_date` is the
    outcome day 'YYYY-MM-DD'.  Lexicographic comparison is chronological for
    zero-padded ISO timestamps.
    """
    return timestamp_utc < settlement_window_open(settlement_date)


# SQL fragment to append to any metar_observations / daily_stats WHERE clause.
# Safe to concatenate after existing conditions (leading "AND").  Binds a single
# `settlement_date` parameter whose value is the outcome day.
CONTEMPORANEOUS_PREDICATE = " AND timestamp_utc < ?"


def asof_where(clause: str) -> str:
    """Return `clause` with the observational as-of predicate appended.

    Pass an existing WHERE fragment (without the leading 'WHERE').  The result is
    `clause` + ` AND timestamp_utc < ?`.  The caller MUST append the settlement
    date as the final bound parameter.
    """
    return clause + CONTEMPORANEOUS_PREDICATE
