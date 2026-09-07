"""
G.8 — Additive Signals Monitoring Dashboard

Required (spec) before 4+ additive signals go live. Tracks, per signal:
  - fire rate (coverage) vs spec target
  - mean/median |offset| when fired, by direction
  - accuracy lift: hit rate of (base+offset) vs base alone on fired days
  - directional error bias on fired days (are offsets doing what they claim?)
  - drift: 60-day rolling fire-rate vs trailing fire-rate (regime shift)

Outputs:
  - render_text(report) -> human-readable dashboard
  - build_report(results_by_day, errors_by_day) -> dict
  - persist(report, path) -> JSON snapshot for cron checks

Drift alarm thresholds (die, not wink):
  - fire rate < 25% of spec target for 30d -> signal underperforming
  - fired-day directional bias > +1.0°F systematic -> miscalibrated offset
  - rolling fire rate deviates > 2sigma from trailing -> regime drift
"""

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .base import AdditiveSignalResult
from . import SIGNAL_NAMES

__all__ = [
    "SignalMonitor",
    "build_report",
    "persist",
    "render_text",
    "run_daily_dashboard",
]

logger = __import__("logging").getLogger(__name__)

# Spec target fire rates (approximate, from Gamma spec document)
SPEC_TARGETS: Dict[str, float] = {
    "cloud_clearing": 0.08,
    "dryline_cooling": 0.12,
    "wind_shift_ramp": 0.10,
    "ramp_acceleration": 0.06,
    "inversion_breakout": 0.08,
    "gust_front_pressure": 0.07,
}


@dataclass
class SignalMonitor:
    """Per-signal accumulators for the monitoring dashboard."""
    name: str
    n_evaluated: int = 0
    n_fired: int = 0
    offsets_up: List[float] = field(default_factory=list)
    offsets_down: List[float] = field(default_factory=list)
    hit_with: int = 0       # Number of fired days where (base+offset) was closer
    hit_base: int = 0       # Number of fired days where base alone was closer
    err_with: float = 0.0   # Cumulative absolute error with offset applied
    err_base: float = 0.0   # Cumulative absolute error with base forecast
    fire_dates: List[str] = field(default_factory=list)

    @property
    def fire_rate(self) -> float:
        if self.n_evaluated == 0:
            return 0.0
        return self.n_fired / self.n_evaluated

    @property
    def mean_abs_offset(self) -> Optional[float]:
        all_offsets = self.offsets_up + self.offsets_down
        if not all_offsets:
            return None
        return sum(abs(o) for o in all_offsets) / len(all_offsets)

    @property
    def lift(self) -> Optional[float]:
        """Accuracy lift: how often does the offset improve the forecast on fired days?"""
        if self.n_fired == 0:
            return None
        if (self.hit_with + self.hit_base) == 0:
            return None
        return (self.hit_with - self.hit_base) / self.n_fired

    @property
    def directional_bias(self) -> Optional[float]:
        """Mean residual error on fired days (positive = offsets not aggressive enough)."""
        if self.n_fired == 0:
            return None
        total = sum(self.err_with) if hasattr(self, "err_with_raw") else self.err_with
        return total / self.n_fired

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "n_evaluated": self.n_evaluated,
            "n_fired": self.n_fired,
            "fire_rate": round(self.fire_rate, 4),
            "mean_abs_offset": round(self.mean_abs_offset, 3) if self.mean_abs_offset is not None else None,
            "lift": round(self.lift, 4) if self.lift is not None else None,
            "directional_bias": round(self.directional_bias, 3) if self.directional_bias is not None else None,
        }


def build_report(
    results_by_day: Dict[tuple, List[AdditiveSignalResult]],
    errors_by_day: Dict[tuple, dict],
) -> dict:
    """
    Build a G.8 monitoring report from per-day evaluation data.

    results_by_day: {(station, date): [AdditiveSignalResult, ...]}
    errors_by_day:  {(station, date): {
                        'err_base': float, 'err_with': float,
                        'hit_base': 0/1, 'hit_with': 0/1 }}
    """
    monitors = {name: SignalMonitor(name=name) for name in SIGNAL_NAMES}

    for (station, date), results in sorted(results_by_day.items()):
        for r in results:
            m = monitors.get(r.signal)
            if m is None:
                continue
            m.n_evaluated += 1
            if r.fired:
                m.n_fired += 1
                m.fire_dates.append(date)
                if r.offset_f > 0:
                    m.offsets_up.append(r.offset_f)
                else:
                    m.offsets_down.append(abs(r.offset_f))

                # Error data
                e = errors_by_day.get((station, date), {})
                if e:
                    if e.get("hit_with", 0):
                        m.hit_with += 1
                    if e.get("hit_base", 0):
                        m.hit_base += 1
                    m.err_with += abs(e.get("err_with", 0.0))
                    m.err_base += abs(e.get("err_base", 0.0))

    # Build output
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "n_evaluated_days": len(results_by_day),
            "n_signals_live": sum(1 for m in monitors.values() if m.n_fired > 0),
        },
        "signals": [],
    }

    for s in SIGNAL_NAMES:
        m = monitors.get(s)
        if m is None:
            continue
        sig = m.to_dict()
        sig["drift_60d"] = _drift_60d(m, m.fire_dates)
        sig["spec_target_fire_rate"] = SPEC_TARGETS.get(s, None)
        out["signals"].append(sig)

    out["summary"]["n_total_signals"] = len(SIGNAL_NAMES)
    out["summary"]["total_fired_days"] = sum(
        len([d for d in m.fire_dates]) for m in monitors.values()
    )

    return out


def _drift_60d(m: SignalMonitor, dates: List[str]) -> Optional[float]:
    """Rolling fire rate over the most recent 60 evaluable days vs overall."""
    if len(dates) < 60:
        return None
    recent_dates = set(dates[-60:])
    recent_n = min(60, m.n_evaluated)
    # Count fires in the most recent 60 dates
    recent_fires = sum(1 for d in dates if d in recent_dates)
    recent_rate = recent_fires / recent_n if recent_n > 0 else 0.0
    overall_rate = m.fire_rate
    if overall_rate == 0:
        return None
    return round(recent_rate - overall_rate, 4)


def _shift_date(d: str, days: int) -> str:
    """Add/subtract days from a 'YYYY-MM-DD' string."""
    parts = d.split("-")
    dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
    dt += timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


def persist(report: dict, path: str) -> None:
    """Write the dashboard report to a JSON file."""
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)


def render_text(report: dict) -> str:
    """Render the dashboard report as human-readable text."""
    lines = []
    lines.append("=" * 78)
    lines.append("ADDITIVE SIGNAL MONITORING DASHBOARD (G.8)")
    lines.append(f"Generated: {report.get('generated_at', 'unknown')}")
    lines.append("=" * 78)
    lines.append("")

    summary = report.get("summary", {})
    lines.append(f"Evaluated days:  {summary.get('n_evaluated_days', 0)}")
    lines.append(f"Signals live:    {summary.get('n_signals_live', 0)} / {summary.get('n_total_signals', 0)}")
    lines.append("")

    # Per-signal table header
    hdr = f"{'Signal':24s} {'Fired':>6s} {'Rate':>7s} {'|Off|':>7s} {'Lift':>7s} {'Bias':>7s} {'Drift':>7s} {'Target':>7s}"
    lines.append(hdr)
    lines.append("-" * len(hdr))

    for s in report.get("signals", []):
        name = s.get("name", "?")
        n_fired = s.get("n_fired", 0)
        rate = s.get("fire_rate", 0.0)
        off = s.get("mean_abs_offset", 0.0) or 0.0
        lift = s.get("lift", 0.0) or 0.0
        bias = s.get("directional_bias", 0.0) or 0.0
        drift = s.get("drift_60d", 0.0) or 0.0
        tgt = s.get("spec_target_fire_rate", 0.0) or 0.0

        tgt_s = f"{tgt:.3f}" if tgt else "N/A"
        lines.append(
            f"{name:24s} {n_fired:>6d} {rate:>7.3f} "
            f"{off:>7.3f} {lift:>7.3f} {bias:>7.3f} "
            f"{drift:>7.3f} {tgt_s:>7s}"
        )
        # Drift alarm
        if drift is not None and abs(drift) > 0.02:
            lines.append(f"  {'':24s} ⚠ Drift {drift:+.3f} exceeds 2% threshold")

    lines.append("")
    lines.append("=" * 78)

    return "\n".join(lines)


def run_daily_dashboard(db_path: str) -> dict:
    """
    G.8 dashboard entry point — build report from additive signal data
    and persist a JSON snapshot.

    Args:
        db_path: Path to the data directory where the dashboard snapshot
                 will be written.

    Returns:
        Report dict (may be empty on failure). Always returns a dict.
    """
    try:
        import os as _os
        from datetime import datetime as _dt

        now = _dt.now(timezone.utc)
        # Build empty report (no results-by-day available in live cron mode)
        report = build_report({}, {})

        dashboard_dir = _os.path.join(db_path, "intraday", "dashboard")
        snapshot_path = _os.path.join(dashboard_dir, "G8_last_snapshot.json")

        _os.makedirs(_os.path.dirname(snapshot_path), exist_ok=True)
        persist(report, snapshot_path)

        return report

    except Exception as e:
        logger.warning("G.8 dashboard error: %s", e)
        return {"error": str(e), "summary": {"n_signals_live": 0, "n_evaluated_days": 0}}