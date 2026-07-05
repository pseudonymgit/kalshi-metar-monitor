#!/usr/bin/env python3
"""
DEV Paper Trading Cron Wrapper (v1.0 — 2026-07-05)

Pure deterministic script — no AI/ML/LLM calls.
Called by cron to run the DEV paper trading instance.

This wrapper:
1. Runs multi_instance_paper_trader.py for DEV instance
2. Writes a completion artifact
3. Writes health status
4. Logs to the DEV instance log

Usage (cron):
    cd /home/node/.openclaw/workspace/prototypes/weather-engine-source && \
    python3 scripts/dev_paper_trading_cron.py

Exit codes:
    0 = success
    1 = lock held (already running)
    2 = runtime error
"""

import sys
import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "core"))

from instance_config import (
    INSTANCE_CONFIGS,
    InstanceLock,
    write_health_status,
    setup_instance_logger,
)


def main():
    instance_name = "DEV"
    cfg = INSTANCE_CONFIGS[instance_name]
    
    logger = setup_instance_logger(instance_name)
    logger.info("DEV paper trading cron started")
    write_health_status(instance_name, "running")
    
    try:
        # Import and run the multi-instance trader
        # (the trader itself handles lock acquisition)
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from multi_instance_paper_trader import MultiInstancePaperTrader
        
        runner = MultiInstancePaperTrader(instances=[instance_name])
        
        # Run for today's date
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        results = runner.run_daily(run_date=today)
        
        # Write completion artifact
        artifact_dir = REPO_ROOT.parent.parent / ".meta" / "continuity" / "weather-engine"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
        artifact_file = artifact_dir / f"dev-cron-completion-{ts}.md"
        
        instance_results = results.get(instance_name, [])
        executed = sum(1 for r in instance_results if r.get('status') == 'executed')
        skipped = sum(1 for r in instance_results if r.get('status') == 'skipped')
        alerts = sum(1 for r in instance_results if r.get('alert_sent'))
        
        with open(artifact_file, 'w') as f:
            f.write(f"# DEV Paper Trading Cron Completion\n\n")
            f.write(f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"**Instance:** DEV\n")
            f.write(f"**Run date:** {today}\n")
            f.write(f"**Deterministic:** Yes (no AI/ML in loop)\n\n")
            f.write(f"## Results\n\n")
            f.write(f"- Executed: {executed}\n")
            f.write(f"- Skipped: {skipped}\n")
            f.write(f"- Alerts sent: {alerts}\n\n")
            f.write(f"## Health\n\n")
            f.write(f"See: {cfg.health_file}\n")
            f.write(f"Log: {cfg.log_path}\n")
        
        logger.info(f"Cron complete: {executed} executed, {skipped} skipped, {alerts} alerts")
        write_health_status(instance_name, "healthy", {
            "run_date": today,
            "executed": executed,
            "skipped": skipped,
            "alerts_sent": alerts,
            "artifact": str(artifact_file),
        })
        
        print(f"[{datetime.now(timezone.utc).isoformat()}] DEV: complete — {executed} executed, {skipped} skipped, {alerts} alerts")
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"Cron failed: {e}", exc_info=True)
        write_health_status(instance_name, "error", {"error": str(e)})
        print(f"[{datetime.now(timezone.utc).isoformat()}] DEV: FAILED — {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
