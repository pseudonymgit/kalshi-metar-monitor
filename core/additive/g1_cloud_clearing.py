"""
G.1 — Cloud Cover Clearing Escape Signal (additive)

Physical edge:
  NWP (and the daily cloud_cover_index signal) scores the whole day from
  morning cloud. When morning overcast is burning off FAST, the standard
  static adjustment underestimates the afternoon solar-insolation recovery.
  A clearing trend by late morning predicts the daily HIGH coming in
  WARMER than the cloud-conditioned expectation -> positive additive offset.

Trigger (all required):
  A. Morning overcast: mean cloud 06:00-10:00 local >= 55%.
  B. Clearing trend: OLS slope of cloud% over [06:00, 10:00] <= -6 %/hr
     (i.e. losing >= ~24% cloud over the morning).
  C. Current state: cloud at 10:00 <= 45% (sun actually getting through).
  D. Dry air check (falsifier): dewpoint depression at 10:00 >= 3degF when
     dewpoint is available — wet overcast burn-off often re-clouds
     (convection), which this signal does NOT trade.

Offset: scale with clearing speed and dryness, capped at +5degF.
"""

from typing import Optional
from .base import AdditiveSignal, AdditiveSignalResult, lin_slope, clamp_f
from .context import AdditiveContext

__all__ = ["CloudClearingSignal"]


class CloudClearingSignal(AdditiveSignal):
    """Detects fast morning cloud burn-off that NWP underestimates."""

    # Trigger thresholds
    OVERCAST_MIN = 55.0       # % cloud mean 06-10
    CLEAR_SLOPE_MAX = -6.0    # %/hr slope cap (<= this = fast clearing)
    CURRENT_MAX = 45.0        # % cloud at 10:00 must be <= this
    DEWPOINT_DEP_MIN = 3.0    # degF dewpoint depression minimum
    PEAK_OFFSET = 5.0         # degF max upward offset

    @property
    def name(self) -> str:
        return "cloud_clearing"

    @property
    def max_offset_f(self) -> float:
        return self.PEAK_OFFSET

    @max_offset_f.setter
    def max_offset_f(self, value: float) -> None:
        self.PEAK_OFFSET = float(value)

    def _dewpoint_depression(self, ctx: AdditiveContext) -> Optional[float]:
        """Compute dewpoint depression at ~10:00 local."""
        obs = ctx.obs_between(9, 11)
        for o in obs:
            if o.temp_f is not None and o.dewpoint_f is not None:
                return o.temp_f - o.dewpoint_f
        return None

    def evaluate(self, ctx: AdditiveContext) -> AdditiveSignalResult:
        # A: Morning overcast
        cloud_hourly = {}
        try:
            cloud_hourly = ctx._wapi_query("cloud", list(range(6, 11)))
        except Exception:
            pass
        if not cloud_hourly:
            return AdditiveSignalResult(signal=self.name, fired=False, reason="no_cloud_data")

        mean_cloud_06_10 = sum(cloud_hourly.values()) / len(cloud_hourly)
        if mean_cloud_06_10 < self.OVERCAST_MIN:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"mean_cloud={mean_cloud_06_10:.1f}% < {self.OVERCAST_MIN}%"
            )

        # B: Clearing trend
        hours = sorted(cloud_hourly.keys())
        cloud_vals = [cloud_hourly[h] for h in hours]
        slope = lin_slope([float(h) for h in hours], cloud_vals)
        if slope is None or slope > self.CLEAR_SLOPE_MAX:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"clearing_slope={slope:.2f} > {self.CLEAR_SLOPE_MAX:.2f} %/hr"
            )

        # C: Current cloud <= 45%
        cloud_at_10 = cloud_hourly.get(max(hours), 100.0)
        if cloud_at_10 > self.CURRENT_MAX:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"cloud_at_10={cloud_at_10:.1f}% > {self.CURRENT_MAX}%"
            )

        # D: Dry air check
        dep = self._dewpoint_depression(ctx)
        if dep is not None and dep < self.DEWPOINT_DEP_MIN:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"dewpoint_depression={dep:.1f}F < {self.DEWPOINT_DEP_MIN}F (wet)"
            )

        # Scale offset: clearing speed * dryness factor
        clearing_speed = abs(slope)  # %/hr
        offset = clamp_f(
            clearing_speed / 3.0,
            0.0,
            self.PEAK_OFFSET,
        )
        return AdditiveSignalResult(
            signal=self.name,
            offset_f=round(offset, 2),
            confidence=min(1.0, offset / self.PEAK_OFFSET + 0.2),
            fired=True,
            reason=(
                f"cloud_06-10={mean_cloud_06_10:.0f}% "
                f"clearing={slope:.1f}%/hr "
                f"at_10={cloud_at_10:.0f}% "
                f"dep={dep:.1f}F"
            ),
            meta={
                "mean_cloud_06_10": round(mean_cloud_06_10, 1),
                "clearing_slope": round(slope, 2),
                "cloud_at_10": round(cloud_at_10, 1),
                "dewpoint_depression": round(dep, 1) if dep is not None else None,
            },
        )