#!/usr/bin/env python3
"""
timing_15m — 15-minute entry/exit timing overlay (Item 2, 2026-09-30).

PURPOSE
-------
The daily distribution layer (`core/intraday/distribution_adapter.py`) answers
*what* strike to trade: P(high >= X) for the day. This module answers *when*
within the day to act, from the 1-minute ASOS archive.

Chain: distribution (strike + P) -> 15m METAR/ASOS overlay -> trigger condition
-> entry signal (station, timestamp, trigger, direction, confidence).

WHAT IT COMPUTES (deterministic math only — NO AI/ML)
-----------------------------------------------------
1. Resample `data/iem_asos_1min.db` (1-min) into strict 15-minute buckets
   (96/day) with, per bucket:
     high_so_far  — running max of bucket temps (monotone non-decreasing)
     rate_15m     — temp change vs the previous bucket
     rate_1h      — temp change vs 4 buckets back (the momentum axis)
     vol_15m      — sample std of the last `vol_lookback` 15m deltas
2. Fire a trigger when the running high is *near the selected strike* and the
   1-hour rate-of-change confirms the direction. Two mirror triggers:
     approach_up — gap = strike - high_so_far in [0, proximity_F] and
                   rate_1h >= +mom_thr_F * vol_mult  -> the high is still
                   climbing toward the strike  -> UP.
     fade_down   — gap > proximity_F and rate_1h <= -mom_thr_F * vol_mult
                   -> the high has peaked short of the strike and is falling
                   -> DOWN.
3. Emit `Timing15mSignal(station, timestamp, trigger, direction, confidence, ...)`.

TRIGGER RATIONALE (derived from the data, 2026-09-30)
-----------------------------------------------------
Measured on 7 stations x ~92 days of real 1-min ASOS (36,579 bucket-states),
targeting P(day's high >= next strike above high_so_far):

    unconditional P(cross)                 = 0.684
    rate_1h >= +0.5 F  -> P(cross)         = 0.725
    rate_1h <= -0.5 F  -> P(cross)         = 0.634
    rate_1h >= +1.0 F  -> P(cross)         = 0.738
    proximity <=1F & rate_1h >= +0.5 F     = 0.733

So 1-hour rate-of-change — not proximity — is the discriminating axis (~9pp
separation at +/-0.5 F). Proximity only says *where* we are; momentum says
*whether we are still going*. The trigger therefore requires both: near the
strike AND moving toward it. Median daily-peak bucket is ~19:00 UTC, so
afternoon momentum is the horizon where the strike is still undecided.

Defaults (`Timing15mParams`):
  proximity_F  = 1.5   — within 1.5 F of the strike the market is ~50/50 and
                         entry timing has the most leverage on price.
  mom_thr_F    = 0.5   — mild threshold; p75 of |1h delta| is ~2.5 F, so this
                         fires often enough to be actionable without being noise.
  vol_mult     = 1.0   — scale the momentum gate by observed 15m volatility so
                         quiet days need less absolute momentum, choppy days more.

COMPOSITION WITH THE HOURLY ENTRY ENGINE
----------------------------------------
`core/intraday/entry_engine.py::EntryTimingEngine.evaluate_hour` decides entry
from probability divergence + confidence + Kelly sizing, but is blind to whether
the intraday trajectory supports the call. This overlay is a *gate/enricher* in
front of it: a timing signal is only actionable when a trigger is live, and its
`confidence` (momentum strength + proximity) can be folded into the engine's
`confidence` argument. The engine still owns sizing; this module owns timing.
Both use the same trading-hours window (14:00-20:00 UTC) so they never disagree
on the session boundary.

Integration sketch (no import of the overlay is required by the engine):

    sig = detect_triggers(buckets, strike)[-1] if ... else None
    if sig is not None and sig.direction match:
        conf = 0.5 * engine_conf + 0.5 * sig.confidence
        entry = EntryTimingEngine().evaluate_hour(hour, hb, p, market_price, conf)

Read-only on every DB. No writes, no AI/ML.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]

# ── Session constants (mirror core/intraday/entry_engine.py) ──────────────────
TRADING_HOUR_START_UTC = 14   # 10:00 ET
TRADING_HOUR_END_UTC = 21     # exclusive; last entry bucket 20:45 UTC (16:45 ET)

BUCKET_MINUTES = 15
BUCKETS_PER_DAY = 24 * 60 // BUCKET_MINUTES  # 96

# ── Trigger defaults ─────────────────────────────────────────────────────────
DEFAULT_PROXIMITY_F = 1.5     # within this many F of the strike
DEFAULT_MOM_THR_F = 0.5       # minimum 1-hour rate-of-change (F/hr)
DEFAULT_VOL_MULT = 1.0        # momentum gate scaled by 15m volatility
DEFAULT_MOM_LOOKBACK = 4      # buckets back for the 1-hour momentum axis
DEFAULT_VOL_LOOKBACK = 8      # 15m deltas in the volatility window

# UNITS (verified 2026-09-30): `iem_asos_1min.db.observations.tmpf` is ALREADY
# Fahrenheit (IEM ASOS API convention). Global range is -49..122; KPHX July reads
# 98..99; KNYC June reads 80 at midnight. Do NOT apply C->F.
# NOTE: scripts/intraday_multifreq_sweep.py:load_intraday_hsf applies `tmpf*9/5+32`
# to this already-F column (e.g. 79F -> 175F), corrupting its w_hsf term.
_ASOS_TMPF_TO_F = lambda v: float(v)


@dataclass(frozen=True)
class Timing15mParams:
    """Tunable, deterministic trigger parameters."""
    proximity_F: float = DEFAULT_PROXIMITY_F
    mom_thr_F: float = DEFAULT_MOM_THR_F
    vol_mult: float = DEFAULT_VOL_MULT
    mom_lookback: int = DEFAULT_MOM_LOOKBACK
    vol_lookback: int = DEFAULT_VOL_LOOKBACK
    trading_start_utc: int = TRADING_HOUR_START_UTC
    trading_end_utc: int = TRADING_HOUR_END_UTC


@dataclass
class Bucket15m:
    """One strict 15-minute bucket of ASOS temperature."""
    index: int              # 0..95 (bucket-of-day)
    start_utc: str          # 'YYYY-MM-DD HH:MM'
    hour_utc: int
    tmpf_mean: float        # mean temp in the bucket (F)
    high_so_far: float      # running max of bucket means up to and incl. this
    rate_15m: Optional[float]   # tmpf_mean - previous bucket mean (F)
    rate_1h: Optional[float]    # tmpf_mean - `mom_lookback` buckets back (F)
    vol_15m: Optional[float]    # std of last `vol_lookback` 15m deltas (F)

    @property
    def is_trading_hour(self) -> bool:
        return TRADING_HOUR_START_UTC <= self.hour_utc < TRADING_HOUR_END_UTC


@dataclass
class Timing15mSignal:
    """An entry/exit timing trigger for one (station, day, bucket)."""
    station: str
    timestamp: str          # bucket start UTC
    trigger: str            # 'approach_up' | 'fade_down'
    direction: str          # 'UP' | 'DOWN'
    confidence: float       # [0, 1] — momentum strength x proximity
    high_so_far: float
    strike: float
    gap: float              # strike - high_so_far (signed)
    rate_1h: float
    vol_15m: float

    def as_dict(self) -> dict:
        return {
            "station": self.station,
            "timestamp": self.timestamp,
            "trigger": self.trigger,
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "high_so_far": round(self.high_so_far, 2),
            "strike": round(self.strike, 2),
            "gap": round(self.gap, 2),
            "rate_1h": round(self.rate_1h, 3),
            "vol_15m": round(self.vol_15m, 3),
        }


# ── Resampling ───────────────────────────────────────────────────────────────

def _bucket_index(valid: str) -> int:
    """'YYYY-MM-DD HH:MM[:SS]' -> 0..95 bucket-of-day. Strict 15-min grid."""
    hh = int(valid[11:13])
    mm = int(valid[14:16])
    return hh * (60 // BUCKET_MINUTES) + mm // BUCKET_MINUTES


def resample_15m(
    rows: Sequence[Tuple[str, float]],
    params: Optional[Timing15mParams] = None,
    expected_buckets: int = BUCKETS_PER_DAY,
) -> List[Bucket15m]:
    """
    Resample 1-min (valid, tmpf_F) rows into strict 15-minute buckets.

    Deterministic aggregation: bucket value = mean of its 1-min temps. `tmpf` is already in F
    (see units note above) — no C->F conversion is applied. Produces a true
    96-bucket grid, unlike the sweep's hour-truncated `high_so_far`.

    Empty buckets are skipped; indices remain the true grid index so gaps are
    visible. `high_so_far` is the running max of bucket means over present buckets.
    """
    p = params or Timing15mParams()
    acc: dict[int, List[float]] = {}
    date = rows[0][0][:10] if rows else ""
    for valid, tmpf in rows:
        if tmpf is None:
            continue
        acc.setdefault(_bucket_index(valid), []).append(float(tmpf))

    out: List[Bucket15m] = []
    highs: List[float] = []          # running max of present bucket means
    means: List[float] = []          # bucket means in order, for rate/vol
    running_max = None
    for idx in sorted(acc):
        vals = acc[idx]
        mean = sum(vals) / len(vals)
        running_max = mean if running_max is None else max(running_max, mean)
        hh = (idx * BUCKET_MINUTES) // 60
        mm = (idx * BUCKET_MINUTES) % 60
        start_utc = f"{date} {hh:02d}:{mm:02d}"

        rate_15m = None
        if means:
            rate_15m = mean - means[-1]

        rate_1h = None
        if len(means) >= p.mom_lookback:
            rate_1h = mean - means[-p.mom_lookback]

        vol_15m = None
        deltas = _deltas(means + [mean])
        if len(deltas) >= p.vol_lookback:
            window = deltas[-p.vol_lookback:]
            vol_15m = _std(window)

        out.append(Bucket15m(
            index=idx, start_utc=start_utc, hour_utc=hh,
            tmpf_mean=mean, high_so_far=running_max,
            rate_15m=rate_15m, rate_1h=rate_1h, vol_15m=vol_15m,
        ))
        means.append(mean)

    return out


def _deltas(series: List[float]) -> List[float]:
    return [series[i] - series[i - 1] for i in range(1, len(series))]


def _std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# ── Data access (READ-ONLY) ──────────────────────────────────────────────────

def _ro_conn(name: str = "iem_asos_1min.db") -> sqlite3.Connection:
    return sqlite3.connect(f"file:{REPO / 'data' / name}?mode=ro", uri=True)


def load_station_day(
    station: str,
    date: str,
    conn: Optional[sqlite3.Connection] = None,
    db_name: str = "iem_asos_1min.db",
) -> List[Tuple[str, float]]:
    """Read one station-day of 1-min temps (F; ASOS tmpf is native F), ascending."""
    own = conn is None
    c = conn or _ro_conn(db_name)
    try:
        rows = c.execute(
            "SELECT valid, tmpf FROM observations "
            "WHERE station=? AND substr(valid,1,10)=? AND tmpf IS NOT NULL "
            "ORDER BY valid",
            (station, date),
        ).fetchall()
    finally:
        if own:
            c.close()
    return [(v, _ASOS_TMPF_TO_F(t)) for v, t in rows]


def buckets_for_station_day(
    station: str,
    date: str,
    params: Optional[Timing15mParams] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Bucket15m]:
    """Load + resample one station-day into 15m buckets."""
    return resample_15m(load_station_day(station, date, conn=conn), params=params)


# ── Trigger ──────────────────────────────────────────────────────────────────

def _confidence(rate_1h: float, gap: float, mom_thr_F: float, vol_mult: float,
                proximity_F: float) -> float:
    """Deterministic confidence in [0,1]: momentum strength x proximity."""
    gate = max(1e-6, mom_thr_F * vol_mult)
    mom_score = min(1.0, abs(rate_1h) / (2.0 * gate))
    prox_score = 1.0 - min(1.0, abs(gap) / max(proximity_F, 1e-6)) if gap >= 0 else 0.5
    return max(0.0, min(1.0, 0.6 * mom_score + 0.4 * prox_score))


def evaluate_bucket(
    bucket: Bucket15m,
    strike: float,
    station: str,
    params: Optional[Timing15mParams] = None,
) -> Optional[Timing15mSignal]:
    """Evaluate a single bucket against the strike; return a signal or None."""
    p = params or Timing15mParams()
    if bucket.rate_1h is None:
        return None
    if not (p.trading_start_utc <= bucket.hour_utc < p.trading_end_utc):
        return None

    gap = strike - bucket.high_so_far
    rate = bucket.rate_1h
    vol = bucket.vol_15m or 0.0
    gate = p.mom_thr_F * p.vol_mult
    conf = _confidence(rate, gap, p.mom_thr_F, p.vol_mult, p.proximity_F)

    # approach_up: at/near the strike, still climbing -> UP
    if 0.0 <= gap <= p.proximity_F and rate >= gate:
        return Timing15mSignal(
            station=station, timestamp=bucket.start_utc, trigger="approach_up",
            direction="UP", confidence=conf, high_so_far=bucket.high_so_far,
            strike=strike, gap=gap, rate_1h=rate, vol_15m=vol,
        )
    # fade_down: strike still above the running high and the high is falling -> DOWN
    if gap > p.proximity_F and rate <= -gate:
        return Timing15mSignal(
            station=station, timestamp=bucket.start_utc, trigger="fade_down",
            direction="DOWN", confidence=conf, high_so_far=bucket.high_so_far,
            strike=strike, gap=gap, rate_1h=rate, vol_15m=vol,
        )
    return None


def detect_triggers(
    buckets: Sequence[Bucket15m],
    strike: float,
    station: str = "",
    params: Optional[Timing15mParams] = None,
) -> List[Timing15mSignal]:
    """All triggers fired across a station-day, in chronological order."""
    out: List[Timing15mSignal] = []
    for b in buckets:
        sig = evaluate_bucket(b, strike, station, params)
        if sig is not None:
            out.append(sig)
    return out


def latest_signal(
    buckets: Sequence[Bucket15m],
    strike: float,
    station: str = "",
    params: Optional[Timing15mParams] = None,
) -> Optional[Timing15mSignal]:
    """Most recent (latest-bucket) trigger, or None — the live decision."""
    for b in reversed(list(buckets)):
        sig = evaluate_bucket(b, strike, station, params)
        if sig is not None:
            return sig
    return None


__all__ = [
    "Timing15mParams",
    "Bucket15m",
    "Timing15mSignal",
    "resample_15m",
    "load_station_day",
    "buckets_for_station_day",
    "evaluate_bucket",
    "detect_triggers",
    "latest_signal",
    "BUCKETS_PER_DAY",
    "TRADING_HOUR_START_UTC",
    "TRADING_HOUR_END_UTC",
]
