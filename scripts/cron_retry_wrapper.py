#!/usr/bin/env python3
"""
cron_retry_wrapper.py — Retry wrapper for cron job scripts

Wraps any script to add retry logic on transient failures.
Usage:
    python3 scripts/cron_retry_wrapper.py --max-retries 3 --retry-delay 60 -- python3 scripts/forecast_disagreement_collector.py

Exits 0 on success, non-zero on persistent failure.
"""

import subprocess
import sys
import time
import os
import argparse


def main():
    parser = argparse.ArgumentParser(description="Retry wrapper for cron scripts")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retry attempts")
    parser.add_argument("--retry-delay", type=int, default=60, help="Delay between retries (seconds)")
    parser.add_argument("--backoff-factor", type=float, default=2.0, help="Exponential backoff multiplier")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run")

    args = parser.parse_args()

    if not args.command:
        print("ERROR: No command specified", file=sys.stderr)
        sys.exit(1)

    # If the command starts with '--', strip it
    cmd = args.command
    if cmd and cmd[0] == '--':
        cmd = cmd[1:]

    if not cmd:
        print("ERROR: No command after -- separator", file=sys.stderr)
        sys.exit(1)

    last_exit_code = 1
    delay = args.retry_delay

    for attempt in range(1, args.max_retries + 1):
        try:
            print(f"[cron_retry] Attempt {attempt}/{args.max_retries}: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=False, timeout=600)
            last_exit_code = result.returncode

            if result.returncode == 0:
                print(f"[cron_retry] Attempt {attempt} succeeded")
                sys.exit(0)

            print(f"[cron_retry] Attempt {attempt} failed (exit code {result.returncode})")

        except subprocess.TimeoutExpired:
            print(f"[cron_retry] Attempt {attempt} timed out")
            last_exit_code = 124
        except FileNotFoundError as e:
            print(f"[cron_retry] Command not found: {e}", file=sys.stderr)
            sys.exit(127)
        except Exception as e:
            print(f"[cron_retry] Attempt {attempt} exception: {e}", file=sys.stderr)
            last_exit_code = 1

        if attempt < args.max_retries:
            print(f"[cron_retry] Waiting {delay}s before retry...")
            time.sleep(delay)
            delay = int(delay * args.backoff_factor)

    print(f"[cron_retry] All {args.max_retries} attempts failed. Last exit code: {last_exit_code}")
    sys.exit(last_exit_code)


if __name__ == "__main__":
    main()