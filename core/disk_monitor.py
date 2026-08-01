"""
Disk Space Monitor — checks available disk before critical operations.

Halts trading if free space falls below configured threshold.

Usage:
    from core.disk_monitor import check_disk_space

    ok, msg = check_disk_space()
    if not ok:
        halt_all_operations(msg)
"""

import os
import logging
import shutil
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# Default thresholds
WARN_THRESHOLD_PCT = 20   # Warning at 20% free
HALT_THRESHOLD_PCT = 10   # HALT at 10% free

# Critical paths to monitor
CRITICAL_PATHS = [
    '/home/node/.openclaw/workspace/prototypes/weather-engine-source/data',
    '/home/node/.openclaw/workspace/prototypes/weather-engine-source',
    '/home/node/.openclaw',
    '/tmp',
]


def get_disk_usage(path: str) -> dict:
    """
    Get disk usage statistics for a given path.

    Returns:
        Dict with total_gb, used_gb, free_gb, free_pct, mount_point
    """
    try:
        usage = shutil.disk_usage(path)
        total_gb = usage.total / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)
        free_pct = (usage.free / usage.total) * 100

        # Get mount point
        mount = path
        while mount and not os.path.ismount(mount):
            mount = os.path.dirname(mount)

        return {
            'path': path,
            'total_gb': round(total_gb, 1),
            'used_gb': round(used_gb, 1),
            'free_gb': round(free_gb, 1),
            'free_pct': round(free_pct, 1),
            'mount_point': mount or path,
        }
    except (OSError, PermissionError) as e:
        logger.error("Cannot check disk usage for %s: %s", path, e)
        return {
            'path': path,
            'error': str(e),
            'free_pct': 100,  # Assume safe if can't check
        }


def check_disk_space(
    paths: Optional[list] = None,
    halt_threshold: float = HALT_THRESHOLD_PCT,
    warn_threshold: float = WARN_THRESHOLD_PCT,
) -> Tuple[bool, str]:
    """
    Check disk space across critical paths.

    Args:
        paths: List of paths to check (default: CRITICAL_PATHS)
        halt_threshold: Percentage free at which to halt (default: 10%)
        warn_threshold: Percentage free at which to warn (default: 20%)

    Returns:
        Tuple of (is_safe: bool, message: str)
        is_safe = False if any path is below halt_threshold
    """
    if paths is None:
        paths = CRITICAL_PATHS

    all_safe = True
    messages = []

    for path in paths:
        if not os.path.exists(path):
            continue

        usage = get_disk_usage(path)

        if 'error' in usage:
            messages.append(f"{path}: {usage['error']}")
            continue

        free_pct = usage['free_pct']

        if free_pct < halt_threshold:
            all_safe = False
            messages.append(
                f"HALT: {path} has {free_pct:.1f}% free "
                f"({usage['free_gb']:.1f}GB / {usage['total_gb']:.1f}GB)"
            )
        elif free_pct < warn_threshold:
            messages.append(
                f"WARN: {path} has {free_pct:.1f}% free "
                f"({usage['free_gb']:.1f}GB / {usage['total_gb']:.1f}GB)"
            )
        else:
            messages.append(
                f"OK: {path} has {free_pct:.1f}% free "
                f"({usage['free_gb']:.1f}GB / {usage['total_gb']:.1f}GB)"
            )

    summary = ' | '.join(messages)
    return all_safe, summary


def pre_trade_disk_check() -> None:
    """
    Check disk before trading. Raises RuntimeError if disk is too full.

    Called at start of daily_paper_run().
    """
    safe, msg = check_disk_space()
    logger.info("pre_trade_disk_check: %s", msg)
    if not safe:
        raise RuntimeError(
            f"Disk space below HALT threshold. Cannot proceed with trading.\n{msg}"
        )


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    paths = sys.argv[1:] if len(sys.argv) > 1 else None
    safe, msg = check_disk_space(paths)
    print(msg)
    sys.exit(0 if safe else 1)