#!/usr/bin/env python3
"""
Weather Engine 5-minute DEV/SBOX Paper Trading Cron Wrapper (v2.0 — 2026-07-13)
B-MODE v2 Compliance: Standard Library Only
Execute multi_instance_paper_trader.py for DEV and SBOX instances.

This wrapper:
1. Runs multi_instance_paper_trader.py for DEV and SBOX instances
2. Writes a completion artifact
3. Logs minimal health status

Usage (cron):
    cd /home/node/.openclaw/workspace/prototypes/weather-engine-source && \
    python3 scripts/dev_paper_trading_cron.py --instances DEV SBOX

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
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--instances', nargs='+', default=['DEV', 'SBOX'],
                       help='Instances to run (PROD, DEV, SBOX)')
    args = parser.parse_args()
    
    # Run for specified instances
    selected_instances = [inst.upper() for inst in args.instances]
    print(f"Running for instances: {', '.join(selected_instances)}")
    
    try:
        # Import and run the multi-instance trader
        # (the trader itself handles lock acquisition)
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from multi_instance_paper_trader import MultiInstancePaperTrader
        
        runner = MultiInstancePaperTrader(instances=selected_instances)
        
        # Run for today's date
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        results = runner.run_daily(run_date=today)
        
        # Write completion artifact
        artifact_dir = REPO_ROOT.parent.parent / ".meta" / "continuity" / "weather-engine"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
        artifact_file = artifact_dir / f"dev-sbox-cron-completion-{ts}.md"
        
        # Calculate summary stats across all requested instances
        total_executed = 0
        total_skipped = 0
        total_alerts = 0
        
        with open(artifact_file, 'w') as f:
            f.write(f"# DEV/SBOX Paper Trading Cron Completion\n\n")
            f.write(f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"**Instances:** {', '.join(selected_instances)}\n")
            f.write(f"**Run date:** {today}\n")
            f.write(f"**Deterministic:** Yes (no AI/ML in loop)\n\n")
            f.write(f"## Results\n\n")
            
            for instance_name in selected_instances:
                if instance_name not in INSTANCE_CONFIGS:
                    print(f"Warning: Unknown instance '{instance_name}'")
                    continue
                    
                cfg = INSTANCE_CONFIGS[instance_name]
                logger = setup_instance_logger(instance_name)
                logger.info(f"{instance_name} paper trading cron started")
                write_health_status(instance_name, "running")
                
                instance_results = results.get(instance_name, [])
                executed = sum(1 for r in instance_results if r.get('status') == 'executed')
                skipped = sum(1 for r in instance_results if r.get('status') == 'skipped')
                alerts = sum(1 for r in instance_results if r.get('alert_sent'))
                
                total_executed += executed
                total_skipped += skipped
                total_alerts += alerts
                
                f.write(f"\n### {instance_name}\n")
                f.write(f"- Executed: {executed}\n")
                f.write(f"- Skipped: {skipped}\n")
                f.write(f"- Alerts sent: {alerts}\n")
                
                logger.info(f"Cron complete: {executed} executed, {skipped} skipped, {alerts} alerts")
                write_health_status(instance_name, "healthy", {
                    "run_date": today,
                    "executed": executed,
                    "skipped": skipped,
                    "alerts_sent": alerts,
                    "artifact": str(artifact_file),
                })
                
                print(f"[{datetime.now(timezone.utc).isoformat()}] {instance_name}: complete — {executed} executed, {skipped} skipped, {alerts} alerts")
            
            f.write(f"\n## Overall Totals\n\n")
            f.write(f"- Total executed: {total_executed}\n")
            f.write(f"- Total skipped: {total_skipped}\n")
            f.write(f"- Total alerts sent: {total_alerts}\n\n")
            
            f.write(f"## Health\n\n")
            f.write(f"Completion artifact: {artifact_file}\n")
            f.write(f"Logs in: {(REPO_ROOT / 'logs').as_posix()}\n")
            
        print(f"Cron completed for all instances. Total: {total_executed} executed, {total_skipped} skipped, {total_alerts} alerts sent")
        sys.exit(0)
        
    except Exception as e:
        print(f"[{datetime.now(timezone.utc).isoformat()}] CRITICAL: FAILED — {e}")
        # Try to write error status for each instance in the specified list
        for instance_name in ['DEV', 'SBOX']:  # Default to DEV and SBOX
            if instance_name in INSTANCE_CONFIGS:
                write_health_status(instance_name, "error", {"error": str(e)})
        sys.exit(2)


if __name__ == "__main__":
    main()
