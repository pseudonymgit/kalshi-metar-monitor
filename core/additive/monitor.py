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
import os
import sqlite3
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
    "persist_results",
    "load_persisted_results",
    "link_t_plus_one_outcomes",
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


def _results_db_path(data_path: str) -> str:
    """Return the additive-results DB path under the supplied data root."""
    return os.path.join(data_path, "intraday", "additive_results.db")


def persist_results(data_path: str, station: str, date: str, results: List[AdditiveSignalResult]) -> int:
    """Persist one station/date additive evaluation batch idempotently."""
    path = _results_db_path(data_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS additive_results (
                station TEXT NOT NULL,
                local_date TEXT NOT NULL,
                signal TEXT NOT NULL,
                offset_f REAL NOT NULL,
                confidence REAL NOT NULL,
                fired INTEGER NOT NULL,
                reason TEXT NOT NULL,
                meta_json TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                PRIMARY KEY (station, local_date, signal)
            )
        """)
        evaluated_at = datetime.now(timezone.utc).isoformat()
        for result in results:
            conn.execute("""
                INSERT INTO additive_results
                    (station, local_date, signal, offset_f, confidence, fired,
                     reason, meta_json, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station, local_date, signal) DO UPDATE SET
                    offset_f=excluded.offset_f,
                    confidence=excluded.confidence,
                    fired=excluded.fired,
                    reason=excluded.reason,
                    meta_json=excluded.meta_json,
                    evaluated_at=excluded.evaluated_at
            """, (station, date, result.signal, result.offset_f,
                  result.confidence, int(result.fired), result.reason,
                  json.dumps(result.meta, sort_keys=True, default=str), evaluated_at))
        conn.commit()
    return len(results)


def load_persisted_results(data_path: str):
    """Load persisted additive results into the dashboard's report shape."""
    path = _results_db_path(data_path)
    if not os.path.exists(path):
        return {}, {}
    results_by_day = {}
    errors_by_day = {}
    with sqlite3.connect(path) as conn:
        rows = conn.execute("""
            SELECT station, local_date, signal, offset_f, confidence, fired, reason, meta_json
            FROM additive_results ORDER BY station, local_date, signal
        """).fetchall()
    for station, local_date, signal, offset_f, confidence, fired, reason, meta_json in rows:
        key = (station, local_date)
        results_by_day.setdefault(key, []).append(AdditiveSignalResult(
            signal=signal, offset_f=offset_f, confidence=confidence,
            fired=bool(fired), reason=reason, meta=json.loads(meta_json or "{}")))
    return results_by_day, errors_by_day


def link_t_plus_one_outcomes(data_path: str) -> dict:
    """Join additive rows to read-only settlement_epochs outcomes.

    The settlement database is evidence-only; only the additive-results DB is
    updated. Rows without a finalized/closed epoch remain explicitly unmatched.
    """
    result_db = _results_db_path(data_path)
    settlement_db = os.path.join(data_path, "metar_backfill.db")
    if not os.path.exists(result_db) or not os.path.exists(settlement_db):
        return {"matched": 0, "unmatched": 0, "source": settlement_db, "available": False}
    with sqlite3.connect(settlement_db) as source:
        outcomes = {(str(st), str(day)): (bucket, status)
                    for st, day, bucket, status in source.execute(
                        "SELECT station, local_trading_date, settlement_bucket, epoch_status "
                        "FROM settlement_epochs "
                        "WHERE settlement_bucket IS NOT NULL AND epoch_status = 'closed'")}
    with sqlite3.connect(result_db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(additive_results)")}
        for name, definition in (
            ("outcome_bucket", "INTEGER"),
            ("outcome_status", "TEXT"),
            ("outcome_source", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE additive_results ADD COLUMN {name} {definition}")
        conn.commit()
    matched = unmatched = 0
    with sqlite3.connect(result_db) as conn:
        rows = conn.execute("SELECT station, local_date FROM additive_results").fetchall()
        for station, day in rows:
            outcome = outcomes.get((station, day))
            if outcome:
                conn.execute("UPDATE additive_results SET outcome_bucket=?, outcome_status=?, outcome_source=? WHERE station=? AND local_date=?",
                             (outcome[0], outcome[1], "metar_backfill.settlement_epochs", station, day))
                matched += 1
            else:
                unmatched += 1
        conn.commit()
    return {"matched": matched, "unmatched": unmatched, "source": settlement_db, "available": True}


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
        t1_receipt = link_t_plus_one_outcomes(db_path)
        results_by_day, errors_by_day = load_persisted_results(db_path)
        report = build_report(results_by_day, errors_by_day)
        report["summary"]["persistence_source"] = _results_db_path(db_path)
        report["summary"]["persistence_rows"] = sum(len(v) for v in results_by_day.values())
        report["summary"]["t_plus_one"] = t1_receipt

        dashboard_dir = _os.path.join(db_path, "intraday", "dashboard")
        snapshot_path = _os.path.join(dashboard_dir, "G8_last_snapshot.json")

        _os.makedirs(_os.path.dirname(snapshot_path), exist_ok=True)
        persist(report, snapshot_path)

        return report

    except Exception as e:
        logger.warning("G.8 dashboard error: %s", e)
        return {"error": str(e), "summary": {"n_signals_live": 0, "n_evaluated_days": 0}}
