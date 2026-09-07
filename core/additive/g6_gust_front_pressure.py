"""
G.6 — Pressure Tendency Gust Front Signal (additive)

Physical edge:
  A sharp barometric fall in the 1-3h window before local noon marks an
  approaching pressure trough / gust front / density interface. The
  temperature response depends on the front's thermodynamic character:

    COLD front (most common in the corridor): post-frontal flow is cooler
      -> daily high lands BELOW the pre-frontal conditioned forecast ->
      offset DOWN.
    WARM front / pre-frontal compressional warming: high lands ABOVE.

  Character is classified from the morning temperature response to the
  same falling pressure — the airmass behind the pressure fall announces
  itself by 11:30 on the thermograph.

Trigger:
  A. 3h pressure fall <= -2.5 mb over [cutoff-3h, cutoff) local
     (dP/dt sign convention: falling pressure = negative delta).
  B. Morning temperature trend classification:
       trend <= -0.4 degF/hr -> cold character -> DOWN
       trend >= +1.8 degF/hr (above the 14d baseline ramp) -> warm character -> UP
       else -> no fire (ambiguous front).
  C. Post-fall wind response present (>= 10 kt by 11:30 or gust report) —
     a silent pressure fall without wind response is usually a weak
     trough that doesn't mix the boundary layer.

Offset: cold -> -(2.5..5degF) scaled with fall rate; warm -> +(1.5..3.5degF).
"""

from typing import List, Optional
from .base import (
    AdditiveSignal, AdditiveSignalResult,
    lin_slope, clamp_f,
)
from .context import AdditiveContext

__all__ = ["GustFrontPressureSignal"]


class GustFrontPressureSignal(AdditiveSignal):
    """Detects gust fronts / pressure troughs affecting afternoon high."""

    # Trigger thresholds
    FALL_MIN_MB = -2.5       # mb — 3h pressure fall minimum
    COLD_TREND_MAX = -0.4    # degF/hr — cold character max trend
    WARM_TREND_MIN = 1.8     # degF/hr — warm character min trend (above baseline)
    WIND_MIN_KT = 10.0       # kt — post-fall wind minimum

    # Offset bounds
    PEAK_COLD = -5.0         # degF max cold offset
    PEAK_WARM = 3.5          # degF max warm offset

    @property
    def name(self) -> str:
        return "gust_front_pressure"

    @property
    def max_offset_f(self) -> float:
        return max(abs(self.PEAK_COLD), abs(self.PEAK_WARM))

    @max_offset_f.setter
    def max_offset_f(self, value: float) -> None:
        self.PEAK_COLD = max(self.PEAK_COLD, float(value))
        self.PEAK_WARM = max(self.PEAK_WARM, float(value))

    def _pressure_fall(
        self, ctx: AdditiveContext,
    ) -> Optional[float]:
        """3h pressure change: pressure now vs 3 hours ago."""
        obs = ctx.obs_between(8, 12)
        press_vals = [(o.local_hour, o.pressure_mb) for o in obs if o.pressure_mb is not None]
        if len(press_vals) < 2:
            return None
        press_vals.sort()
        earliest = press_vals[0][1]
        latest = press_vals[-1][1]
        return latest - earliest

    def _temp_trend(
        self, ctx: AdditiveContext,
        start_hour: int = 8, end_hour: int = 11,
    ) -> Optional[float]:
        """OLS temp trend (degF/hr) over [start_hour, end_hour)."""
        obs = ctx.obs_between(start_hour, end_hour)
        hours = [o.local_hour for o in obs if o.temp_f is not None]
        temps = [o.temp_f for o in obs if o.temp_f is not None]
        if len(hours) < 2:
            return None
        return lin_slope([float(h) for h in hours], temps)

    def _baseline_ramp(self, ctx: AdditiveContext) -> Optional[float]:
        """Trailing 14-day same-morning ramp (degF/hr)."""
        from datetime import datetime as _dt, timedelta
        ramps = []
        today = _dt.strptime(ctx.date_str, "%Y-%m-%d")
        for i in range(1, 15):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            obs = ctx.prior_obs_on(d)
            hours = [o.local_hour for o in obs if o.temp_f is not None and 8 <= o.local_hour < 12]
            temps = [o.temp_f for o in obs if o.temp_f is not None and 8 <= o.local_hour < 12]
            if len(hours) >= 2:
                slope = lin_slope([float(h) for h in hours], temps)
                if slope is not None:
                    ramps.append(slope)
        if not ramps:
            return None
        return sum(ramps) / len(ramps)

    def evaluate(self, ctx: AdditiveContext) -> AdditiveSignalResult:
        # A: Pressure fall
        fall = self._pressure_fall(ctx)
        if fall is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="no_pressure_data"
            )
        if fall > self.FALL_MIN_MB:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"pressure_fall={fall:.2f}mb > {self.FALL_MIN_MB}mb"
            )

        # B: Temperature trend classification
        trend = self._temp_trend(ctx, 8, 11)
        if trend is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="no_temp_trend"
            )

        # C: Post-fall wind response
        obs = ctx.obs_between(10, 12)
        gust_reported = any(
            getattr(o, "meta", {}).get("gust_kt", 0) for o in obs
        )
        max_wind = max(
            (o.wind_speed_kt for o in obs if o.wind_speed_kt is not None),
            default=0.0,
        )
        if max_wind < self.WIND_MIN_KT and not gust_reported:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"no_wind_response (max={max_wind:.0f}kt < {self.WIND_MIN_KT}kt)"
            )

        # Classify and scale offset
        fall_rate = abs(fall)  # mb

        if trend <= self.COLD_TREND_MAX:
            # Cold front: offset DOWN
            offset = clamp_f(
                -(fall_rate * 1.5),
                self.PEAK_COLD,
                0.0,
            )
            char = "cold"
        elif trend >= self.WARM_TREND_MIN:
            # Warm front: check ramp anomaly vs baseline
            baseline = self._baseline_ramp(ctx)
            if baseline is None or trend > baseline:
                offset = clamp_f(
                    fall_rate * 1.0,
                    0.0,
                    self.PEAK_WARM,
                )
                char = "warm"
            else:
                return AdditiveSignalResult(
                    signal=self.name, fired=False,
                    reason=f"warm_trend={trend:.2f} but below baseline"
                )
        else:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"ambiguous_trend={trend:.2f}F/hr"
            )

        confidence = min(1.0, abs(offset) / 5.0 + 0.1)

        return AdditiveSignalResult(
            signal=self.name,
            offset_f=round(offset, 2),
            confidence=confidence,
            fired=True,
            reason=(
                f"{char}_front fall={fall:.2f}mb trend={trend:.2f}F/hr "
                f"wind={max_wind:.0f}kt offset={offset:+.1f}F"
            ),
            meta={
                "pressure_fall_mb": round(fall, 2),
                "temp_trend_F_per_hr": round(trend, 3),
                "character": char,
                "max_wind_kt": round(max_wind, 0),
                "gust_reported": gust_reported,
            },
        )