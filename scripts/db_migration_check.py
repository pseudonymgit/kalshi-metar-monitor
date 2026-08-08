#!/usr/bin/env python3
"""
DB Migration Audit — Verify migration from raw sqlite3.connect() to core.db_utils.

Usage:
    python3 scripts/db_migration_check.py                         # full audit
    python3 scripts/db_migration_check.py --db-migration          # same
    python3 scripts/db_migration_check.py --db-migration --verbose  # with line numbers

Counts unmanaged `sqlite3.connect()` calls across `core/` and `scripts/`,
excluding test files, archives, backup directories, and the utility modules
themselves (db_utils.py, db_connection.py, sqlite_utils.py).
"""

import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATED_FILES = [
    "core/station_registry.py",
    "core/station_skill_gate.py",
    "core/adaptive_thresholds.py",
    "core/trajectory_confirmation_gate.py",
    "core/whale_watch_db.py",
    "scripts/big_sweep.py",
    "scripts/compute_signal_correlation_matrix.py",
]

EXCLUDED_DIRS = {
    "tests", "archive", "core.backup", "core.backup.20260617_phase3_discovery",
    ".venv", "__pycache__",
}
EXCLUDED_FILES = {
    "db_utils.py", "db_connection.py", "sqlite_utils.py",
}
ALLOWED_PATTERNS = [
    r"from core\.db_utils import",
]


def is_excluded(path: str) -> bool:
    """Check if a path should be skipped."""
    rel = os.path.relpath(path, REPO_ROOT)
    parts = rel.replace("\\", "/").split("/")
    for part in parts:
        if part in EXCLUDED_DIRS:
            return True
    if parts and parts[-1] in EXCLUDED_FILES:
        return True
    return False


def count_raw_connects(filepath: str) -> list:
    """Count raw sqlite3.connect() calls in a file. Returns list of line numbers."""
    matches = []
    try:
        with open(filepath) as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return matches

    # Skip comment-only lines and lines in docstrings
    lines = content.split("\n")
    in_docstring = False
    for lineno, line in enumerate(lines, 1):
        # Track multi-line docstrings
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            if '"""' in stripped[3:] or "'''" in stripped[3:]:
                continue  # single-line docstring
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue

        # Skip comments
        if stripped.startswith("#"):
            continue

        # Look for raw sqlite3.connect() — not wrapped in with_db, query_db, etc.
        # Match: sqlite3.connect(  (not preceded by with_db, query_db, execute_db)
        if re.search(r'(?<![\.\w])sqlite3\.connect\(', stripped):
            matches.append(lineno)

    return matches


def check_file_uses_db_utils(filepath: str) -> bool:
    """Check if a file imports from core.db_utils."""
    try:
        with open(filepath) as f:
            content = f.read()
        return "from core.db_utils import" in content
    except (OSError, UnicodeDecodeError):
        return False


def audit_python_files(directories: list, verbose: bool = False):
    """Audit all Python files in directories for raw sqlite3.connect()."""
    all_files = []
    for d in directories:
        abs_d = os.path.join(REPO_ROOT, d)
        if not os.path.isdir(abs_d):
            continue
        for root, dirs, files in os.walk(abs_d):
            # Skip excluded dirs
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for f in files:
                if f.endswith(".py"):
                    all_files.append(os.path.join(root, f))

    total_raw = 0
    raw_files = []
    migrated_ok = 0
    migrated_broken = 0

    print("=" * 72)
    print("  DB MIGRATION AUDIT")
    print("=" * 72)
    print()

    for fp in sorted(all_files):
        rel = os.path.relpath(fp, REPO_ROOT)
        if is_excluded(fp):
            continue

        uses_db_utils = check_file_uses_db_utils(fp)
        raw_connects = count_raw_connects(fp)
        n_raw = len(raw_connects)

        if n_raw > 0:
            if verbose:
                print(f"  ⚠️  {rel}: {n_raw} raw connect(s) at lines {raw_connects}")
            else:
                print(f"  ⚠️  {rel}: {n_raw} raw connect(s)")
            total_raw += n_raw
            raw_files.append(rel)
        elif uses_db_utils:
            print(f"  ✅ {rel}: migrated (uses db_utils)")

    # Check the nine explicitly-migrated files
    print()
    print("─" * 72)
    print("  Migrated file status:")
    print("─" * 72)
    for mf in MIGRATED_FILES:
        fp = os.path.join(REPO_ROOT, mf)
        if not os.path.exists(fp):
            print(f"  ❌ {mf}: FILE NOT FOUND")
            migrated_broken += 1
            continue
        raw = count_raw_connects(fp)
        uses = check_file_uses_db_utils(fp)
        if uses and len(raw) == 0:
            print(f"  ✅ {mf}: clean — uses db_utils, no raw connects")
            migrated_ok += 1
        elif uses:
            print(f"  ⚠️  {mf}: uses db_utils but {len(raw)} raw connect(s) remain: {raw}")
            migrated_broken += 1
        else:
            print(f"  ❌ {mf}: does NOT import from db_utils")
            migrated_broken += 1

    print()
    print("=" * 72)
    print(f"  SUMMARY")
    print("=" * 72)
    print(f"  Files scanned:     {len(all_files)}")
    print(f"  Migrated OK:       {migrated_ok}")
    print(f"  Migrated broken:   {migrated_broken}")
    print(f"  Raw connects (all): {total_raw} in {len(raw_files)} files")
    if raw_files:
        print()
        print("  Unmigrated files:")
        for rf in raw_files:
            print(f"    - {rf}")
    print()

    return total_raw == 0 and migrated_broken == 0


def main():
    parser = argparse.ArgumentParser(description="DB Migration Audit")
    parser.add_argument("--db-migration", action="store_true", default=True,
                        help="Run the migration audit")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show line numbers")
    parser.add_argument("--dirs", nargs="*", default=["core", "scripts"],
                        help="Directories to scan (default: core scripts)")
    args = parser.parse_args()

    if not args.db_migration:
        # Allow the script to be imported and called without side effects
        return

    success = audit_python_files(args.dirs, verbose=args.verbose)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()