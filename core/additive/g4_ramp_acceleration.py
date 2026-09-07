"""
G.4 — Diurnal Ramp Acceleration Signal (additive)

Spec premise: the slope of the morning ramp in the first hours of heating
predicts whether the day's high will land above or below the
morning-conditioned expectation. Mechanism: the ramp slope embeds the day's
actual insolation/mixing regime — a sluggish early ramp under a supposedly
sunny forecast means haze/smoke/moist soils capping heating (high comes in
LOW); an unusually aggressive ramp means dry, transparent air and deep mixing
overshooting the standard curve (high comes in HIGH).

Window: 3 hours of morning heating before the no-lookahead cutoff —
[08:30, 11:30) local.

Trigger: POSITIVE ramp anomaly only (slope above the station's trailing-14-day
same-window baseline by >= 1.5 degF/hr).
"""

from typing import List, Optional
from .base import AdditiveSignal, AdditiveSignalResult, lin_slope, clamp_f
from .context import AdditiveContext

__all__ = ["RampAccelerationSignal"]


class RampAccelerationSignal(AdditiveSignal):
    """Detects unusually aggressive morning ramp predicting higher highs."""

    ANOMALY_MIN = 1.5    # degF/hr — ramp vs baseline anomaly threshold
    PEAK_OFFSET = 4.0    # degF max upward offset

    @property
    def name(self) -> str:
        return "ramp_acceleration"

    @property
    def max_offset_f(self) -> float:
        return self.PEAK_OFFSET

    @max_offset_f.setter
    def max_offset_f(self, value: float) -> None:
        self.PEAK_OFFSET = float(value)

    def _baseline_ramp(self, ctx: AdditiveContext) -> Optional[float]:
        """Trailing 14-day same-morning [08:30, 11:30) ramp (degF/hr)."""
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
        # Compute today's ramp slope in [08:30, 11:30) local
        obs = ctx.obs_between(8, 12)
        hours = [o.local_hour for o in obs if o.temp_f is not None]
        temps = [o.temp_f for o in obs if o.temp_f is not None]
        if len(hours) < 2:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="insufficient_temp_obs"
            )

        today_ramp = lin_slope([float(h) for h in hours], temps)
        if today_ramp is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="ramp_slope_failed"
            )

        # Compare to trailing baseline
        baseline = self._baseline_ramp(ctx)
        if baseline is None:
            return AdditiveSignalResult(
                signal=self.name, fired=False, reason="no_baseline"
            )

        ramp_anomaly = today_ramp - baseline
        if ramp_anomaly < self.ANOMALY_MIN:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason=f"ramp_anomaly={ramp_anomaly:.2f}F/hr < {self.ANOMALY_MIN}F/hr "
                       f"(ramp={today_ramp:.2f}, baseline={baseline:.2f})"
            )

        # Scale offset: anomaly ratio * peak
        offset = clamp_f(
            ramp_anomaly / self.ANOMALY_MIN * 2.0,
            0.0,
            self.PEAK_OFFSET,
        )
        confidence = min(1.0, offset / self.PEAK_OFFSET + 0.15)

        return AdditiveSignalResult(
            signal=self.name,
            offset_f=round(offset, 2),
            confidence=confidence,
            fired=True,
            reason=(
                f"ramp={today_ramp:.2f}F/hr anomaly={ramp_anomaly:.2f}F/hr "
                f"offset={offset:+.1f}F"
            ),
            meta={
                "today_ramp": round(today_ramp, 3),
                "baseline_ramp": round(baseline, 3),
                "ramp_anomaly": round(ramp_anomaly, 3),
            },
        )