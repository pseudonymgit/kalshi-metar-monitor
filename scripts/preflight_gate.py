#!/usr/bin/env python3
"""
Absorption Prevention — Pre-Flight Gate

Prevents the weather engine from being absorbed into implementation work
without a proper spec. Enforces the AGENTS.md rules:

A. No agent shall implement a signal, feature, or system change without a
   written spec filed in docs/plans/.
B. No agent shall backfill data without a written plan specifying the data
   source, expected size, and expected cost.
C. No agent shall modify the paper trading or production trading pathway
   without a written risk assessment.
D. No agent shall deploy code to production without a written deployment
   plan.

B-Mode compliant. No AI/ML.

Usage:
    python3 scripts/preflight_gate.py --operation signal --name "my_new_signal"
    python3 scripts/preflight_gate.py --operation backfill --name "hrrr_backfill"
    python3 scripts/preflight_gate.py --operation trading --name "paper_trading_change"
    python3 scripts/preflight_gate.py --operation deploy --name "prod_deploy_v2"

Returns exit code 0 if gate passes, 1 if blocked.
"""
import os
import sys
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLANS_DIR = REPO_ROOT / "docs" / "plans"

# Rules from AGENTS.md
RULES = {
    "signal": {
        "description": "Signal/feature/system change",
        "spec_pattern": "**/*SIGNAL*",
        "rule": "A: No implementation without a written spec",
        "fallback": "Write a spec in docs/plans/ first",
    },
    "backfill": {
        "description": "Data backfill",
        "spec_pattern": "**/*BACKFILL*",
        "rule": "B: No backfill without a written plan",
        "fallback": "Write a backfill plan with data source, size, and cost",
    },
    "trading": {
        "description": "Paper/production trading pathway change",
        "spec_pattern": "**/*TRADING*",
        "rule": "C: No trading pathway change without risk assessment",
        "fallback": "Write a risk assessment in docs/plans/",
    },
    "deploy": {
        "description": "Production deployment",
        "spec_pattern": "**/*DEPLOY*",
        "rule": "D: No deploy without a deployment plan",
        "fallback": "Write a deployment plan in docs/plans/",
    },
}


def check_spec_exists(operation: str, name: str) -> bool:
    """Check if a spec file exists in docs/plans/ matching the operation and name."""
    name_lower = name.lower().replace("_", "-").replace(" ", "-")
    # Check for exact match
    for f in PLANS_DIR.glob(f"*{name_lower}*"):
        return True
    # Check for partial match
    for f in PLANS_DIR.glob(f"*{operation}*"):
        if name_lower in f.stem.lower():
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Pre-flight gate for weather engine changes")
    parser.add_argument("--operation", required=True, choices=list(RULES.keys()),
                        help="Type of operation")
    parser.add_argument("--name", required=True,
                        help="Name of the change (matched against docs/plans/ files)")
    parser.add_argument("--skip", action="store_true",
                        help="Skip the gate (for emergency overrides)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print detailed information")

    args = parser.parse_args()
    rule = RULES[args.operation]

    if args.skip:
        print(f"⚠️  GATE SKIPPED: {args.operation} '{args.name}' — emergency override")
        sys.exit(0)

    if check_spec_exists(args.operation, args.name):
        print(f"✅ GATE PASSED: {args.operation} '{args.name}' — spec found")
        sys.exit(0)
    else:
        print(f"❌ GATE BLOCKED: {args.operation} '{args.name}'")
        print(f"   Rule {rule['rule']}")
        print(f"   {rule['description']}")
        print(f"   {rule['fallback']}")
        if args.verbose:
            print(f"\n   Existing specs in {PLANS_DIR}:")
            for f in sorted(PLANS_DIR.glob("*.md")):
                print(f"     - {f.name}")
        sys.exit(1)


if __name__ == "__main__":
    main()