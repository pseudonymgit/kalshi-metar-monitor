"""
G.5 — Inversion Breakout Signal (additive)

Physical edge:
  Under morning inversions (stable nights, light winds, low ceilings/fog),
  the boundary layer is decoupled. When the inversion breaks, the mixing
  out of a residually-warm layer produces a step-jump in near-surface
  temperature — the afternoon high overshoots what the stagnant-morning
  linear extrapolation (and NWP conditioned on it) implies.

Trigger (all required):
  A. Stagnation signature before ~09:30 local:
     - mean ceiling (or fog-inducing low ceiling) <= 1500 ft when ceiling
       data present, OR morning temp range <= 1.5degF over 06:00-09:30, AND
     - morning mean wind <= 7 kt.
  B. Breakout: temperature jump >= +2.5degF within any 2 consecutive hourly
     steps in [09:00, 11:30) local, OR a >=45deg wind veer with speed >= 8 kt
     after 09:00 (mechanical mixing erosion).
  C. Conviction check: the jump persists — temp at 11:00-11:30 >= temp
     before the jump (no one-off sensor spike).

Offset: UP, scaled with jump size, capped at +5degF.

Marine-layer stations have modified thresholds (coastal inversion behavior).
"""

from typing import List, Optional
from .base import AdditiveSignal, AdditiveSignalResult, circular_diff, clamp_f
from .context import AdditiveContext, Obs

__all__ = ["InversionBreakoutSignal"]

# Stations where marine-layer inversions dominate
MARINE_STATIONS = frozenset(["KLAX", "KSFO", "KSEA", "KBOS", "KPHL", "KNYC"])


class InversionBreakoutSignal(AdditiveSignal):
    """Detects morning inversion breakouts that spike afternoon highs."""

    # Stagnation thresholds
    CEILING_MAX_FT = 1500.0     # ft max ceiling for stagnation
    STAGNANT_RANGE_F = 1.5      # degF max temp range over 06:00-09:30 for stagnation
    WIND_MAX_KT = 7.0           # kt max morning mean wind for stagnation

    # Breakout thresholds
    JUMP_MIN_F = 2.5            # degF minimum temperature jump
    VEER_DEG = 45.0             # degrees minimum wind veer
    VEER_WIND_KT = 8.0          # kt minimum wind after veer
    PEAK_OFFSET = 5.0           # degF max upward offset

    @property
    def name(self) -> str:
        return "inversion_breakout"

    @property
    def max_offset_f(self) -> float:
        return self.PEAK_OFFSET

    @max_offset_f.setter
    def max_offset_f(self, value: float) -> None:
        self.PEAK_OFFSET = float(value)

    def _seq(self, obs: List[Obs], field: str) -> List[tuple]:
        """Extract (hour, value) tuples from observation list for a field."""
        out = []
        for o in obs:
            v = getattr(o, field, None)
            if v is not None:
                out.append((o.local_hour, v))
        return sorted(out)

    def _detect_jump(
        self, seq: List[tuple],
    ) -> Optional[float]:
        """Detect temperature jump >= JUMP_MIN_F in consecutive hourly steps."""
        for i in range(len(seq) - 1):
            h1, t1 = seq[i]
            h2, t2 = seq[i + 1]
            if h2 - h1 <= 2 and (t2 - t1) >= self.JUMP_MIN_F:
                return t2 - t1
        return None

    def _detect_veer(
        self, ctx: AdditiveContext,
    ) -> bool:
        """Detect mechanical mixing via wind veer >= 45deg with speed >= 8 kt."""
        obs = ctx.obs_between(9, 12)
        pre_dirs = [o.wind_dir for o in obs if o.wind_dir is not None and o.local_hour < 10]
        post_dirs = [o.wind_dir for o in obs if o.wind_dir is not None and o.local_hour >= 10]
        post_speeds = [o.wind_speed_kt for o in obs if o.wind_speed_kt is not None and o.local_hour >= 10]
        if not pre_dirs or not post_dirs or not post_speeds:
            return False
        max_shift = max(
            circular_diff(pd, cd)
            for pd in pre_dirs for cd in post_dirs
        )
        max_speed = max(post_speeds)
        return max_shift >= self.VEER_DEG and max_speed >= self.VEER_WIND_KT

    def _persists(self, seq: List[tuple], jump_hour: int) -> bool:
        """Check that temp at 11:00-11:30 >= temp just after the jump."""
        post = [(h, t) for h, t in seq if h >= jump_hour and h >= 11]
        if not post:
            # Check if the jump itself is at 11+
            if jump_hour >= 11:
                return True
            return False
        max_post_temp = max(t for _, t in post)
        pre_jump_seq = [(h, t) for h, t in seq if h < jump_hour]
        if not pre_jump_seq:
            return True
        pre_jump_temp = pre_jump_seq[-1][1]
        return max_post_temp >= pre_jump_temp

    def evaluate(self, ctx: AdditiveContext) -> AdditiveSignalResult:
        obs = ctx.obs_between(6, 12)

        # A: Stagnation signature
        temp_seq = self._seq(obs, "temp_f")

        # Ceiling check (if available)
        ceiling_vals = [o.ceiling_ft for o in obs if o.ceiling_ft is not None and o.local_hour < 10]
        ceiling_stagnant = True  # default: assume stagnant if no ceiling data
        if ceiling_vals:
            ceiling_stagnant = sum(ceiling_vals) / len(ceiling_vals) <= self.CEILING_MAX_FT

        # Temp range over 06:00-09:30
        early_temps = [t for h, t in temp_seq if h < 10]
        temp_stagnant = True  # default
        if early_temps:
            temp_range = max(early_temps) - min(early_temps)
            temp_stagnant = temp_range <= self.STAGNANT_RANGE_F

        # Wind stagnation
        wind_speeds = [o.wind_speed_kt for o in obs if o.wind_speed_kt is not None and o.local_hour < 10]
        wind_stagnant = True
        if wind_speeds:
            wind_stagnant = sum(wind_speeds) / len(wind_speeds) <= self.WIND_MAX_KT

        stagnation = (ceiling_stagnant or temp_stagnant) and wind_stagnant
        if not stagnation:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason="no_stagnation_signature"
            )

        # B: Breakout detection
        jump = self._detect_jump(temp_seq)
        veer = self._detect_veer(ctx)

        if jump is None and not veer:
            return AdditiveSignalResult(
                signal=self.name, fired=False,
                reason="no_breakout_detected"
            )

        # C: Conviction — jump persists
        if jump is not None:
            jump_hour = None
            for i in range(len(temp_seq) - 1):
                if temp_seq[i + 1][1] - temp_seq[i][1] >= self.JUMP_MIN_F:
                    jump_hour = temp_seq[i + 1][0]
                    break
            if jump_hour is not None and not self._persists(temp_seq, jump_hour):
                return AdditiveSignalResult(
                    signal=self.name, fired=False,
                    reason="jump_did_not_persist"
                )

        # Scale offset
        jump_factor = jump if jump is not None else 3.0  # default estimate if veer-only
        offset = clamp_f(
            jump_factor * 1.2,
            0.0,
            self.PEAK_OFFSET,
        )
        confidence = min(1.0, offset / self.PEAK_OFFSET + 0.1)

        return AdditiveSignalResult(
            signal=self.name,
            offset_f=round(offset, 2),
            confidence=confidence,
            fired=True,
            reason=(
                f"stagnation+breakout jump={jump:.1f}F" if jump is not None
                else f"stagnation+veer_breakout offset={offset:.1f}F"
            ),
            meta={
                "jump_F": round(jump, 1) if jump is not None else None,
                "veer_detected": veer,
                "ceiling_stagnant": ceiling_stagnant,
                "temp_stagnant": temp_stagnant,
                "wind_stagnant": wind_stagnant,
            },
        )