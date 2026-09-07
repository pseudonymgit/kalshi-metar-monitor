"""
G.3 — AM Wind Shift / Temperature Ramp Correlation Signal (additive)

Empirical premise: a wind direction shift before 10AM local marks an airmass
exchange whose afternoon evolution the morning-conditioned forecast
systematically mishandles — the ramp prediction error roughly doubles on
shift days. The SIGN of the resulting high error is set by what the new
airmass is doing to the temperature trend.

Mechanics:
  1. Compute pre-shift mean wind (05-08 local) vs current wind (08-10
     local). A shift = circular direction change >= 40deg.
  2. Classify the shift's thermal character from the SAME morning data:
       - COLD advection: temp trend 08-11 local <= -0.5 degF/hr -> offset DOWN.
       - WARM advection: temp trend >= +0.5 degF/hr steeper than the
         trailing-7-day same-morning baseline ramp -> offset UP.
       - Ambiguous (|trend| < 0.5 degF/hr): no fire.
  3. Offset magnitude scales with shift magnitude and ramp anomaly,
     capped at +/-4degF.
"""

from typing import List, Optional
from .base import (
    AdditiveSignal, AdditiveSignalResult,
    circular_mean_deg, circular_diff, lin_slope, clamp_f,
)
from .context import AdditiveContext

__all__ = ["AmWindShiftSignal"]


class AmWindShiftSignal(AdditiveSignal):
    """Detects AM wind shifts that signal airmass exchange."""

    SHIFT_DEG = 40.0       # Minimum wind direction change (degrees)
    RAMP_TREND_MIN = 0.5   # degF/hr ramp trend threshold
    PEAK_OFFSET = 4.0      # degF max offset magnitude

    @property
    def name(self) -> str:
        return "wind_shift_ramp"

    @property
    def max_offset_f(self) -> float:
        return self.PEAK_OFFSET

    @max_offset_f.setter
    def max_offset_f(self, value: float) -> None:
        self.PEAK_OFFSET = float(value)

    def _temp_trend(
        self, ctx: AdditiveContext,
        start_hour: int, end_hour: int,
    ) -> Optional[float]:
        """OLS temp trend (degF/hr) over local-hour window."""
        obs = ctx.obs_between(start_hour, end_hour)
        if len(obs) < 2:
            return None
        hours = [o.local_hour for o in obs if o.temp_f is not None]
        temps = [o.temp_f for o in obs if o.temp_f is not None]
        if len(hours) < 2:
            return None
        return lin_slope([float(h) for h in hours], temps)

    def _baseline_morning_ramp(self, ctx: AdditiveContext) -> Optional[float]:
        """Trailing 7-day same-morning baseline ramp (degF/hr)."""
        from datetime import datetime as _dt, timedelta
        ramps = []
        today = _dt.strptime(ctx.date_str, "%Y-%m-%d")
        for i in range(1, 8):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            obs = ctx.prior_obs_on(d)
            hours = [o.local_hour for o in obs if o.temp_f is not None and 8 <= o.local_hour <= 11]
            temps = [o.temp_f for o in obs if o.temp_f is not None and 8 <= o.local_hour <= 11]
            if len(hours) >= 2:
                r = lin_slope([float(h) for h in hours], temps)
                if r is not None:
                    ramps.append(r)
        if not ramps:
            return None
        return sum(ramps) / len(ramps)

    def evaluate(self, ctx: AdditiveContext) -> AdditiveSignalResult:
        # 1. Pre-shift wind (05-08 local) vs current wind (08-10 local)
        pre_obs = ctx.obs_between(5, 8)
        cur_obs = ctx.obs_between(8, 10)
        if len(pre_obs) < 1 or len(cur_obs) < 1:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="insufficient_wind_obs"
            )

        pre_dirs = [o.wind_dir for o in pre_obs if o.wind_dir is not None]
        cur_dirs = [o.wind_dir for o in cur_obs if o.wind_dir is not None]
        if not pre_dirs or not cur_dirs:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="no_wind_direction"
            )

        pre_mean = circular_mean_deg(pre_dirs)
        cur_mean = circular_mean_deg(cur_dirs)
        if pre_mean is None or cur_mean is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="circular_mean_failed"
            )

        shift = circular_diff(pre_mean, cur_mean)
        if shift < self.SHIFT_DEG:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"shift={shift:.0f}deg < {self.SHIFT_DEG}deg"
            )

        # 2. Thermal character from temp trend 08-11 local
        trend = self._temp_trend(ctx, 8, 11)
        if trend is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="no_temp_trend"
            )

        # Determine offset sign and magnitude
        offset_mag = clamp_f(shift / self.SHIFT_DEG * self.PEAK_OFFSET, 0, self.PEAK_OFFSET)

        if trend <= -self.RAMP_TREND_MIN:
            # Cold advection -> DOWN
            offset = -offset_mag
            char = "cold"
        elif trend >= self.RAMP_TREND_MIN:
            # Warm advection -> UP (above baseline)
            baseline = self._baseline_morning_ramp(ctx)
            ramp_anomaly = trend - (baseline or 0)
            if ramp_anomaly < self.RAMP_TREND_MIN:
                return AdditiveSignalResult(
                    signal=self.name, fired=False,
                    reason=f"ramp_anomaly={ramp_anomaly:.2f}F/hr below threshold (trend={trend:.2f}, baseline={baseline:.2f})"
                )
            offset = offset_mag
            char = "warm"
        else:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"ambiguous_trend={trend:.2f}F/hr (|trend| < {self.RAMP_TREND_MIN})"
            )

        offset = clamp_f(offset, -self.PEAK_OFFSET, self.PEAK_OFFSET)
        confidence = min(1.0, shift / (self.SHIFT_DEG * 2) + abs(trend) / 3.0)

        return AdditiveSignalResult(
            signal=self.name,
            offset_f=round(offset, 2),
            confidence=confidence,
            fired=True,
            reason=(
                f"shift={shift:.0f}deg {char} trend={trend:.2f}F/hr "
                f"offset={offset:+.1f}F"
            ),
            meta={
                "pre_mean_dir": round(pre_mean, 0) if pre_mean is not None else None,
                "cur_mean_dir": round(cur_mean, 0) if cur_mean is not None else None,
                "shift_deg": round(shift, 0),
                "trend_F_per_hr": round(trend, 3),
                "character": char,
            },
        )