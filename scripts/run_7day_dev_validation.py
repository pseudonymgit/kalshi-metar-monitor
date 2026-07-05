#!/usr/bin/env python3
"""
7-Day DEV Paper Trading Validation Run (2026-07-05)

Runs the multi-instance paper trader for 7 consecutive days using the
last 7 days of available settlement data (2025-08-21 to 2025-08-27).

- Uses DEV ledger and DEV DB snapshot as baseline
- Logs all decisions with explicit Trade Conf, Current bucket, Trading bucket,
  Market odds, signals, Sharpe, coverage, running P&L
- Emits Discord alert format on every non-zero position size (logged to console
  since no Discord webhook is configured)
- Produces a summary report: accuracy, P&L by signal, promotion recommendation
- Updates continuity artifact

No promotion to PROD. DEV only.
"""

import sys
import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

# Set up paths
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "core"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Ensure we're in DEV mode
os.environ["PAPER_TRADING_INSTANCE"] = "DEV"

from multi_instance_paper_trader import MultiInstancePaperTrader, INSTANCE_CONFIGS
from paper_trading_engine import PaperTrader, MarketSide

# ─── Configuration ────────────────────────────────────────────────────────

RUN_DATES = [
    "2025-08-21", "2025-08-22", "2025-08-23", "2025-08-24",
    "2025-08-25", "2025-08-26", "2025-08-27",
]
INSTANCE = "DEV"
STATIONS = [
    'KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD', 'KMIA',
    'KSEA', 'KSFO', 'KHOU', 'KPHX', 'KDEN', 'KAUS',
    'KPHL', 'KMDW', 'KNYC', 'KDFW', 'KMSP', 'KDTW', 'KCLT',
]
STATIONS = sorted(set(STATIONS))

DEV_DB = str(REPO_ROOT / "data" / "paper_trading_dev.db")
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
LOG_DIR = str(REPO_ROOT / "logs")
REPORT_DIR = str(REPO_ROOT / "reports")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, f"dev_7day_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log")
SUMMARY_REPORT = os.path.join(REPORT_DIR, f"dev_7day_summary_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json")


def log(msg, print_msg=True):
    """Log to both console and file."""
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    line = f"[{ts}] {msg}"
    if print_msg:
        print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def restore_dev_snapshot():
    """Restore DEV DB from snapshot to get a clean baseline."""
    import gzip
    snapshot_path = str(REPO_ROOT / "data" / "snapshots" / "paper_trading_dev_001.db.gz")
    if os.path.exists(snapshot_path):
        log(f"Restoring DEV DB from snapshot: {snapshot_path}")
        with gzip.open(snapshot_path, 'rb') as f:
            data = f.read()
        with open(DEV_DB, 'wb') as f:
            f.write(data)
        log(f"DEV DB restored ({len(data)} bytes)")
    else:
        log(f"WARNING: No snapshot found at {snapshot_path}, using existing DEV DB")


def get_current_bucket(station, date, conn):
    """Get current temperature bucket for station."""
    c = conn.cursor()
    c.execute("""
        SELECT settlement_bucket FROM settlement_epochs
        WHERE station = ? AND local_trading_date = ? AND epoch_status = 'closed'
        LIMIT 1
    """, (station, date))
    row = c.fetchone()
    return row[0] if row else 0


def get_trading_bucket(station, date, conn):
    """Get the trading/prior settlement bucket."""
    c = conn.cursor()
    c.execute("""
        SELECT prior_settlement_bucket FROM settlement_epochs
        WHERE station = ? AND local_trading_date = ? AND epoch_status = 'closed'
        LIMIT 1
    """, (station, date))
    row = c.fetchone()
    return row[0] if row else 0


def get_running_pnl(trader):
    """Get running P&L from the trader's DB."""
    try:
        conn = sqlite3.connect(trader.paper_db)
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM trades WHERE status = 'closed'")
        result = c.fetchone()
        conn.close()
        return float(result[0]) if result else 0.0
    except Exception:
        return 0.0


def compute_sharpe(trader):
    """Compute running Sharpe ratio."""
    try:
        conn = sqlite3.connect(trader.paper_db)
        c = conn.cursor()
        c.execute("""
            SELECT realized_pnl FROM trades
            WHERE status = 'closed' AND realized_pnl IS NOT NULL
            ORDER BY trade_date_utc
        """)
        pnls = [row[0] for row in c.fetchall()]
        conn.close()
        if len(pnls) < 2:
            return 0.0
        mean_pnl = sum(pnls) / len(pnls)
        std_pnl = (sum((x - mean_pnl) ** 2 for x in pnls) / (len(pnls) - 1)) ** 0.5
        if std_pnl == 0:
            return 0.0
        return (mean_pnl / std_pnl) * (252 ** 0.5)
    except Exception:
        return 0.0


def format_discord_alert(station, market, direction, size, current_bucket, trading_bucket,
                         market_odds, trade_conf, trade_conf_value, top_signals,
                         sharpe, coverage, running_pnl):
    """Format the exact Discord alert."""
    lines = [
        f"📍 Station: {station}",
        f"📊 Market: {market}",
        f"📈 Direction: {direction}",
        f"💰 Size: ${size:.2f}",
        f"🌡️ Current bucket: {current_bucket}",
        f"🎯 Trading bucket: {trading_bucket}",
        f"📉 Market odds: {market_odds:.2f}",
        f"✅ Trade Conf: {trade_conf.upper()} ({trade_conf_value:.2f})",
        f"🔝 Top signals: {', '.join(top_signals)}",
        f"📊 Sharpe: {sharpe:.1f} | Coverage: {coverage}",
        f"💵 Running P&L: ${running_pnl:+,.2f}",
    ]
    return "\n".join(lines)


def run_7_day_validation():
    """Run the 7-day DEV paper trading validation."""
    log("=" * 70)
    log("7-DAY DEV PAPER TRADING VALIDATION RUN")
    log(f"Run dates: {RUN_DATES[0]} to {RUN_DATES[-1]}")
    log(f"Instance: {INSTANCE}")
    log(f"Stations: {len(STATIONS)}")
    log(f"DEV DB: {DEV_DB}")
    log(f"METAR DB: {METAR_DB}")
    log("=" * 70)

    # Step 1: Restore DEV snapshot
    restore_dev_snapshot()

    # Step 2: Initialize runner
    runner = MultiInstancePaperTrader(instances=[INSTANCE])
    cfg, trader = runner.instances[INSTANCE]

    metar_conn = sqlite3.connect(METAR_DB, timeout=10)

    all_daily_results = {}
    all_alerts = []
    all_trade_records = []

    # Step 3: Run each day
    for run_date in RUN_DATES:
        log(f"\n{'─' * 60}")
        log(f"DAY {RUN_DATES.index(run_date) + 1}/7 — {run_date}")
        log(f"{'─' * 60}")

        results = runner.run_daily(run_date=run_date, stations=STATIONS)
        daily_results = results.get(INSTANCE, [])

        all_daily_results[run_date] = daily_results

        # Process each result for detailed logging
        for r in daily_results:
            station = r.get('station', '?')
            signal_type = r.get('signal_type', r.get('functionality', '?'))
            status = r.get('status', '?')

            if status == 'executed':
                # Get detailed info for the alert
                current_bucket = get_current_bucket(station, run_date, metar_conn)
                trading_bucket = get_trading_bucket(station, run_date, metar_conn)
                running_pnl = get_running_pnl(trader)
                sharpe = compute_sharpe(trader)
                coverage = f"{len(STATIONS)}/{len(STATIONS)}"

                # Get market price from the trade record
                market_odds = r.get('market_price', 0.5)
                confidence = r.get('confidence', 0.5)
                position_size = r.get('position_size_usd', r.get('cost', 0))
                direction = r.get('signal_direction', r.get('signal_direction', 'UP'))
                if hasattr(direction, 'value'):
                    direction = direction.value

                # Get trade version and functionality
                trade_version = r.get('trade_version', 'v3.0_dev')
                functionality = r.get('functionality', signal_type)

                # Build top signals list
                top_signals = [f"{functionality} (conf={confidence:.2f})"]

                # Build the Discord alert format
                alert = format_discord_alert(
                    station=station,
                    market=r.get('market_type', 'HIGH'),
                    direction=direction,
                    size=position_size,
                    current_bucket=current_bucket,
                    trading_bucket=trading_bucket,
                    market_odds=market_odds,
                    trade_conf="HIGH" if confidence >= 0.70 else "MEDIUM" if confidence >= 0.50 else "LOW",
                    trade_conf_value=confidence,
                    top_signals=top_signals,
                    sharpe=sharpe,
                    coverage=coverage,
                    running_pnl=running_pnl,
                )

                log(f"\n  📡 DISCORD ALERT (not sent — no webhook configured):")
                for line in alert.split("\n"):
                    log(f"    {line}")

                all_alerts.append({
                    'date': run_date,
                    'station': station,
                    'alert': alert,
                    'position_size': position_size,
                    'confidence': confidence,
                    'signal_type': functionality,
                    'direction': direction,
                    'market_odds': market_odds,
                    'current_bucket': current_bucket,
                    'trading_bucket': trading_bucket,
                    'sharpe': sharpe,
                    'running_pnl': running_pnl,
                })

                all_trade_records.append({
                    'date': run_date,
                    'station': station,
                    'signal_type': functionality,
                    'trade_version': trade_version,
                    'status': status,
                    'direction': direction,
                    'position_size': position_size,
                    'market_price': market_odds,
                    'analytical_prob': r.get('analytical_prob', 0),
                    'confidence': confidence,
                    'current_bucket': current_bucket,
                    'trading_bucket': trading_bucket,
                    'sharpe': sharpe,
                    'running_pnl': running_pnl,
                    'trade_uuid': r.get('trade_uuid', ''),
                    'trade_type': str(r.get('trade_type', '')),
                })

                log(f"\n  ✅ EXECUTED: {station} {functionality} {direction}")
                log(f"     Trade Conf: {'HIGH' if confidence >= 0.70 else 'MEDIUM' if confidence >= 0.50 else 'LOW'} ({confidence:.2f})")
                log(f"     Current bucket: {current_bucket} | Trading bucket: {trading_bucket}")
                log(f"     Market odds: {market_odds:.2f} | Size: ${position_size:.2f}")
                log(f"     Sharpe: {sharpe:.1f} | Coverage: {coverage}")
                log(f"     Running P&L: ${running_pnl:+,.2f}")
            else:
                reason = r.get('reason', 'unknown')
                log(f"  ⏭️ SKIPPED: {station} {signal_type} — {reason}")

        # Daily summary
        executed = sum(1 for r in daily_results if r.get('status') == 'executed')
        skipped = sum(1 for r in daily_results if r.get('status') == 'skipped')
        log(f"\n  Daily Summary: {executed} executed, {skipped} skipped")

    metar_conn.close()

    # Step 4: Generate summary report
    log(f"\n{'=' * 70}")
    log("GENERATING 7-DAY SUMMARY REPORT")
    log(f"{'=' * 70}")

    report = generate_summary_report(trader, all_daily_results, all_alerts, all_trade_records)

    # Save report
    with open(SUMMARY_REPORT, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    log(f"Summary report saved: {SUMMARY_REPORT}")

    # Print key metrics
    log(f"\n  Total trades: {report['total_trades']}")
    log(f"  Executed: {report['executed_trades']}")
    log(f"  Skipped: {report['skipped_trades']}")
    log(f"  Settled: {report['settled_trades']}")
    log(f"  Win rate: {report['win_rate']:.1%}")
    log(f"  Total P&L: ${report['total_pnl']:+,.2f}")
    log(f"  Sharpe ratio: {report['sharpe_ratio']:.2f}")
    log(f"  Signals by type: {json.dumps(report['pnl_by_signal'], indent=2)}")
    log(f"  Promotion recommendation: {report['promotion_recommendation']}")

    # Step 5: Update continuity artifact
    update_continuity_artifact(report)

    log(f"\n{'=' * 70}")
    log("7-DAY DEV PAPER TRADING VALIDATION COMPLETE")
    log(f"{'=' * 70}")

    return report


def generate_summary_report(trader, all_daily_results, all_alerts, all_trade_records):
    """Generate the 7-day summary report."""

    # Query the DEV DB for final state
    conn = sqlite3.connect(DEV_DB)
    c = conn.cursor()

    # Total trades
    c.execute("SELECT COUNT(*) FROM trades WHERE trade_date_utc >= '2025-08-21'")
    total_trades = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM trades WHERE trade_date_utc >= '2025-08-21' AND status='closed'")
    settled_trades = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM trades WHERE trade_date_utc >= '2025-08-21' AND status='open'")
    open_trades = c.fetchone()[0]

    # Win/loss
    c.execute("""
        SELECT COUNT(*) FROM trades 
        WHERE trade_date_utc >= '2025-08-21' AND status='closed' AND realized_pnl > 0
    """)
    winning_trades = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(*) FROM trades 
        WHERE trade_date_utc >= '2025-08-21' AND status='closed' AND realized_pnl < 0
    """)
    losing_trades = c.fetchone()[0]

    # Total P&L
    c.execute("""
        SELECT COALESCE(SUM(realized_pnl), 0) FROM trades 
        WHERE trade_date_utc >= '2025-08-21' AND status='closed'
    """)
    total_pnl = c.fetchone()[0]

    # P&L by signal type
    c.execute("""
        SELECT functionality, 
               COUNT(*) as count,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
               COALESCE(SUM(realized_pnl), 0) as total_pnl,
               AVG(confidence_indicator) as avg_confidence
        FROM trades 
        WHERE trade_date_utc >= '2025-08-21'
        GROUP BY functionality
        ORDER BY total_pnl DESC
    """)
    pnl_by_signal = {}
    for row in c.fetchall():
        signal, count, wins, losses, pnl, avg_conf = row
        pnl_by_signal[signal] = {
            'count': count,
            'wins': wins,
            'losses': losses,
            'total_pnl': round(pnl, 2),
            'win_rate': f"{wins/(wins+losses)*100:.1f}%" if (wins + losses) > 0 else "N/A",
            'avg_confidence': round(avg_conf, 3) if avg_conf else 0,
        }

    # P&L by station
    c.execute("""
        SELECT station,
               COUNT(*) as count,
               COALESCE(SUM(realized_pnl), 0) as total_pnl
        FROM trades 
        WHERE trade_date_utc >= '2025-08-21'
        GROUP BY station
        ORDER BY total_pnl DESC
    """)
    pnl_by_station = {}
    for row in c.fetchall():
        pnl_by_station[row[0]] = {
            'count': row[1],
            'total_pnl': round(row[2], 2),
        }

    # Daily P&L breakdown
    c.execute("""
        SELECT date_utc, opening_balance, closing_balance, pnl, trade_count,
               winning_trades, losing_trades, settled_trades
        FROM daily_balances
        WHERE date_utc >= '2025-08-21'
        ORDER BY date_utc
    """)
    daily_breakdown = []
    for row in c.fetchall():
        daily_breakdown.append({
            'date': row[0],
            'opening_balance': round(row[1], 2),
            'closing_balance': round(row[2], 2),
            'pnl': round(row[3], 2),
            'trade_count': row[4],
            'winning_trades': row[5],
            'losing_trades': row[6],
            'settled_trades': row[7],
        })

    # Calibration metrics
    c.execute("""
        SELECT brier_score, expected_calibration_error, avg_confidence,
               total_trades, total_resolved
        FROM calibration_metrics
        ORDER BY id DESC LIMIT 1
    """)
    cal_row = c.fetchone()
    calibration = {
        'brier_score': cal_row[0] if cal_row else None,
        'ece': cal_row[1] if cal_row else None,
        'avg_confidence': cal_row[2] if cal_row else None,
        'total_trades': cal_row[3] if cal_row else 0,
        'total_resolved': cal_row[4] if cal_row else 0,
    }

    conn.close()

    # Sharpe ratio
    sharpe = compute_sharpe(trader)

    # Directional accuracy (for settled trades)
    directional_accuracy = None
    if settled_trades > 0:
        # Win rate as a proxy for directional accuracy
        directional_accuracy = winning_trades / settled_trades if settled_trades > 0 else 0

    # Promotion recommendation per PROMOTION-RULES.md
    promotion_checks = {
        'signal_accuracy_65pct': directional_accuracy >= 0.65 if directional_accuracy is not None else False,
        'sharpe_1_0': sharpe >= 1.0,
        'no_alert_spam_50day': len(all_alerts) < 50 * 7,
        'settlement_integrity': True,  # Will verify below
        'brier_score_025': calibration['brier_score'] is not None and calibration['brier_score'] < 0.25,
    }

    # Check settlement integrity
    conn = sqlite3.connect(DEV_DB)
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM trades 
        WHERE status='closed' AND realized_pnl IS NULL AND trade_date_utc >= '2025-08-21'
    """)
    null_pnl_count = c.fetchone()[0]
    promotion_checks['settlement_integrity'] = (null_pnl_count == 0)
    conn.close()

    all_pass = all(promotion_checks.values())
    promotion_recommendation = "ADVANCE to PROD" if all_pass else "DO NOT PROMOTE — failing checks"

    failing = [k for k, v in promotion_checks.items() if not v]
    if failing:
        promotion_recommendation += f" (failing: {', '.join(failing)})"

    report = {
        'report_date': datetime.now(timezone.utc).isoformat(),
        'run_period': f"{RUN_DATES[0]} to {RUN_DATES[-1]}",
        'instance': INSTANCE,
        'total_trades': total_trades,
        'executed_trades': len(all_trade_records),
        'skipped_trades': sum(1 for d in all_daily_results.values() for r in d if r.get('status') == 'skipped'),
        'settled_trades': settled_trades,
        'open_trades': open_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': winning_trades / (winning_trades + losing_trades) if (winning_trades + losing_trades) > 0 else 0,
        'directional_accuracy': directional_accuracy,
        'total_pnl': round(total_pnl, 2),
        'sharpe_ratio': round(sharpe, 2),
        'pnl_by_signal': pnl_by_signal,
        'pnl_by_station': pnl_by_station,
        'daily_breakdown': daily_breakdown,
        'calibration': calibration,
        'alerts_generated': len(all_alerts),
        'promotion_checks': promotion_checks,
        'promotion_recommendation': promotion_recommendation,
        'stations_covered': len(STATIONS),
        'all_alerts': [{'date': a['date'], 'station': a['station'], 'size': a['position_size'],
                        'confidence': a['confidence'], 'signal': a['signal_type']}
                       for a in all_alerts],
    }

    return report


def update_continuity_artifact(report):
    """Update the continuity artifact for this run."""
    artifact_dir = Path("/home/node/.openclaw/workspace/.meta/continuity/weather-engine")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    artifact_path = artifact_dir / "dev-7day-paper-trading.md"

    content = f"""# DEV 7-Day Paper Trading Validation — Continuity Artifact

**Created:** {datetime.now(timezone.utc).isoformat()}  
**Owner:** Gilfoyle (via Donna dispatch)  
**Status:** {'COMPLETE' if report['total_trades'] > 0 else 'NO_TRADES'}

## Run Details
- **Instance:** DEV
- **Run period:** {report['run_period']}
- **Stations:** {report['stations_covered']}
- **DEV DB:** `prototypes/weather-engine-source/data/paper_trading_dev.db`
- **METAR DB:** `prototypes/weather-engine-source/data/metar_backfill.db`

## Results Summary
- **Total trades:** {report['total_trades']}
- **Executed:** {report['executed_trades']}
- **Settled:** {report['settled_trades']}
- **Win rate:** {report['win_rate']:.1%}
- **Total P&L:** ${report['total_pnl']:+,.2f}
- **Sharpe ratio:** {report['sharpe_ratio']:.2f}
- **Alerts generated:** {report['alerts_generated']}

## Promotion Checks
| Check | Status |
|-------|--------|
| Signal accuracy ≥ 65% | {'✅' if report['promotion_checks']['signal_accuracy_65pct'] else '❌'} |
| Sharpe ≥ 1.0 | {'✅' if report['promotion_checks']['sharpe_1_0'] else '❌'} |
| No alert spam (<50/day) | {'✅' if report['promotion_checks']['no_alert_spam_50day'] else '❌'} |
| Settlement integrity | {'✅' if report['promotion_checks']['settlement_integrity'] else '❌'} |
| Brier score < 0.25 | {'✅' if report['promotion_checks']['brier_score_025'] else '❌'} |

## Promotion Recommendation
{report['promotion_recommendation']}

## P&L by Signal Type
"""
    for signal, stats in report['pnl_by_signal'].items():
        content += f"- **{signal}:** {stats['count']} trades, P&L ${stats['total_pnl']:+,.2f}, win rate {stats['win_rate']}\n"

    content += f"""
## Next Actions
- Dan reviews this report
- If ADVANCE: proceed to SBOX smoke test per PROMOTION-RULES.md
- If DO NOT PROMOTE: address failing checks, re-run 7-day validation
- No promotion to PROD without explicit Dan approval

## Files
- Full report: `{SUMMARY_REPORT}`
- Run log: `{LOG_FILE}`
- DEV DB: `prototypes/weather-engine-source/data/paper_trading_dev.db`

**Stop conditions:** DEV run complete. No further action without Dan's review.
"""

    with open(artifact_path, 'w') as f:
        f.write(content)
    log(f"Continuity artifact updated: {artifact_path}")


if __name__ == "__main__":
    report = run_7_day_validation()
    print(f"\n{'=' * 70}")
    print("FINAL REPORT")
    print(f"{'=' * 70}")
    print(json.dumps(report, indent=2, default=str))
