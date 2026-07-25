#!/usr/bin/env python3
"""
PROD Paper Trading Cron Wrapper (v1.0 — 2026-07-05)

Pure deterministic script — no AI/ML/LLM calls.
Called by cron to run the PROD paper trading instance.

This wrapper:
1. Runs multi_instance_paper_trader.py for PROD instance
2. Writes a completion artifact
3. Writes health status
4. Logs to the PROD instance log

Usage (cron):
    cd /home/node/.openclaw/workspace/prototypes/weather-engine-source && \
    python3 scripts/prod_paper_trading_cron.py

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


# ─── Halt file check ───
def _check_halt_before_start() -> bool:
    """Check for halt file before starting. Returns True if halted."""
    halt_path = REPO_ROOT / "data" / ".halt"
    if halt_path.exists():
        reason = ""
        try:
            reason = halt_path.read_text().strip()
        except Exception:
            reason = "unknown"
        print(f"[HALT] HALTED — {reason}")
        return True
    return False


def main():
    instance_name = "PROD"
    cfg = INSTANCE_CONFIGS[instance_name]
    
    # Check halt file before proceeding
    if _check_halt_before_start():
        print("[HALT] Cron skipped due to halt file")
        sys.exit(0)
    
    logger = setup_instance_logger(instance_name)
    logger.info("PROD paper trading cron started")
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
        artifact_file = artifact_dir / f"prod-cron-completion-{ts}.md"
        
        instance_results = results.get(instance_name, [])
        executed = sum(1 for r in instance_results if r.get('status') == 'executed')
        skipped = sum(1 for r in instance_results if r.get('status') == 'skipped')
        alerts = sum(1 for r in instance_results if r.get('alert_sent'))
        
        with open(artifact_file, 'w') as f:
            f.write(f"# PROD Paper Trading Cron Completion\n\n")
            f.write(f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"**Instance:** PROD\n")
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
        
        print(f"[{datetime.now(timezone.utc).isoformat()}] PROD: complete — {executed} executed, {skipped} skipped, {alerts} alerts")
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"Cron failed: {e}", exc_info=True)
        write_health_status(instance_name, "error", {"error": str(e)})
        print(f"[{datetime.now(timezone.utc).isoformat()}] PROD: FAILED — {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()