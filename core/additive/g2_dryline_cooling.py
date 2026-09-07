"""
G.2 — Dry-Line Evaporative Cooling Signal (additive)

Physical edge:
  When a dry line passes (dewpoint drops sharply with winds veering to the
  dry side), afternoon highs are SUPPRESSED 3-5degF relative to a moist
  airmass with the same morning temps: more solar energy goes to
  evaporation (bowen ratio drop) than sensible heating. NWP handles this
  poorly when the front timing straddles the forecast cycle -> the daily
  HIGH lands BELOW the conditioned forecast -> negative additive offset.

Trigger (all required):
  A. Warm season: month in {4..9}.
  B. Plains stations (dry-line climatological corridor).
  C. Dewpoint drop: morning dewpoint (06-10 local mean) >= 5degF below
     YESTERDAY MORNING (06-10 local mean) — diurnally matched comparison.
  D. Dry-veer signature: current (09-11 local) wind direction is within
     the station's dry sector AND mean morning wind >= 8 kt (mixing
     present to advect the dry air).

Offset: proportional to dewpoint drop, direction DOWN, capped at -5degF.
"""

from typing import List, Optional
from .base import AdditiveSignal, AdditiveSignalResult, circular_diff, clamp_f
from .context import AdditiveContext

__all__ = ["DrylineCoolingSignal"]

# Plains stations: dry-line climatological corridor
PLAINS_STATIONS = [
    "KDFW", "KOKC", "KSAT", "KAUS", "KDEN", "KPHX",
]

# Dry line active only in warm months
PLAINS_ACTIVE = list(range(4, 10))  # April-September

# Station-specific dry sectors (bearing range in degrees where dry air advection occurs)
DRY_SECTORS = {
    "KDFW": (270, 360),   # NW-N
    "KOKC": (270, 360),   # NW-N
    "KSAT": (270, 360),   # NW-N
    "KAUS": (270, 360),
    "KDEN": (270, 360),
    "KPHX": (180, 270),   # W-SW
}

WARM_MONTHS = {4, 5, 6, 7, 8, 9}


class DrylineCoolingSignal(AdditiveSignal):
    """Detects dry-line passage suppressing afternoon highs."""

    DEWPOINT_DROP_MIN = 5.0   # degF minimum morning dewpoint drop vs yesterday
    MIN_WIND_KT = 8.0         # kt minimum wind speed for advection
    PEAK_DROP = -5.0          # degF max downward offset

    @property
    def name(self) -> str:
        return "dryline_cooling"

    @property
    def max_offset_f(self) -> float:
        return abs(self.PEAK_DROP)

    @max_offset_f.setter
    def max_offset_f(self, value: float) -> None:
        self.PEAK_DROP = abs(float(value))

    def _mean_field(
        self, ctx: AdditiveContext, field: str,
        start_hour: int, end_hour: int,
    ) -> Optional[float]:
        """Mean of a METAR observation field over a local-hour window."""
        vals = []
        for o in ctx.obs_between(start_hour, end_hour):
            v = getattr(o, field, None)
            if v is not None:
                vals.append(v)
        if not vals:
            return None
        return sum(vals) / len(vals)

    def _in_sector(self, station: str, wind_dir: float) -> bool:
        """Check if wind direction falls within the station's dry sector."""
        sector = DRY_SECTORS.get(station)
        if sector is None:
            return False
        lo, hi = sector
        if lo <= hi:
            return lo <= wind_dir <= hi
        else:
            return wind_dir >= lo or wind_dir <= hi

    def evaluate(self, ctx: AdditiveContext) -> AdditiveSignalResult:
        station = ctx.station

        # A: Warm season
        month = int(ctx.date_str.split("-")[1])
        if month not in WARM_MONTHS:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"month={month} not in warm season"
            )

        # B: Plains station
        if station not in PLAINS_STATIONS:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"{station} not a plains station"
            )

        # C: Dewpoint drop vs yesterday morning (diurnally matched)
        dp_today = self._mean_field(ctx, "dewpoint_f", 6, 10)
        if dp_today is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="no_dewpoint_today"
            )

        # Get yesterday's morning dewpoint
        yesterday = ctx.date_str
        try:
            parts = ctx.date_str.split("-")
            from datetime import datetime as _dt, timedelta
            yesterday_dt = _dt(int(parts[0]), int(parts[1]), int(parts[2])) - timedelta(days=1)
            yesterday_str = yesterday_dt.strftime("%Y-%m-%d")
        except Exception:
            yesterday_str = ""

        dp_yesterday = None
        prior = ctx.prior_obs_on(yesterday_str) if yesterday_str else []
        y_vals = [o.dewpoint_f for o in prior if o.dewpoint_f is not None and 6 <= o.local_hour < 10]
        if y_vals:
            dp_yesterday = sum(y_vals) / len(y_vals)

        if dp_yesterday is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="no_yesterday_dewpoint"
            )

        dp_drop = dp_yesterday - dp_today
        if dp_drop < self.DEWPOINT_DROP_MIN:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"dewpoint_drop={dp_drop:.1f}F < {self.DEWPOINT_DROP_MIN}F"
            )

        # D: Dry-veer signature
        wind_dir_mean = self._mean_field(ctx, "wind_dir", 9, 11)
        wind_speed_mean = self._mean_field(ctx, "wind_speed_kt", 6, 10)
        if wind_dir_mean is None or wind_speed_mean is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="no_wind_data"
            )
        if not self._in_sector(station, wind_dir_mean):
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"wind_dir={wind_dir_mean:.0f} not in dry sector"
            )
        if wind_speed_mean < self.MIN_WIND_KT:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"wind_speed={wind_speed_mean:.1f}kt < {self.MIN_WIND_KT}kt"
            )

        # Offset proportional to dewpoint drop
        offset = clamp_f(
            -(dp_drop / 2.0),
            self.PEAK_DROP,
            0.0,
        )
        confidence = min(1.0, dp_drop / (self.DEWPOINT_DROP_MIN * 2))
        return AdditiveSignalResult(
            signal=self.name,
            offset_f=round(offset, 2),
            confidence=confidence,
            fired=True,
            reason=(
                f"dp_drop={dp_drop:.1f}F "
                f"wind={wind_dir_mean:.0f}deg@{wind_speed_mean:.0f}kt "
                f"offset={offset:.1f}F"
            ),
            meta={
                "dp_today": round(dp_today, 1),
                "dp_yesterday": round(dp_yesterday, 1),
                "dp_drop": round(dp_drop, 1),
                "wind_dir": round(wind_dir_mean, 0),
                "wind_speed": round(wind_speed_mean, 1),
            },
        )