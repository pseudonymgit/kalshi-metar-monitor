#!/usr/bin/env python3
"""
Convert raw sqlite3.connect() calls to use centralized sqlite_utils.
"""
import re
import os

SKIP_FILES = {
    "core/db_schema.py",
    "core/db_health_monitor.py",
    "core/sqlite_utils.py",
    "core/p3_db_migration.py",
    "core/dashboard.py",  # already converted above
    "_convert_db.py",
}


def _find_import_insert_line(lines):
    """Find a good line to insert the sqlite_utils import."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip docstrings, comments, shebang, encoding
        if stripped.startswith('"""') or stripped.startswith("'''") or stripped.startswith('#') or stripped.startswith('from __future__'):
            continue
        # Find the first actual import statement
        if stripped.startswith('import ') or stripped.startswith('from '):
            # Find the last import line
            last_import = i
            for j in range(i, min(i + 30, len(lines))):
                ls = lines[j].strip()
                if ls.startswith('import ') or ls.startswith('from ') or ls == '' or ls.startswith('#'):
                    if ls.startswith('import ') or ls.startswith('from '):
                        last_import = j
                else:
                    break
            return last_import + 1
    return 0


def _file_has_connect_usage(content):
    """Check if file has sqlite3.connect() usage in actual code (not just comments/docstrings)."""
    # Simple check - if 'sqlite3.connect' appears in the file
    return 'sqlite3.connect' in content


def convert_file(filepath):
    relpath = os.path.relpath(filepath)
    if relpath in SKIP_FILES:
        print(f"  SKIP (excluded): {relpath}")
        return False

    with open(filepath, 'r') as f:
        content = f.read()

    if not _file_has_connect_usage(content):
        return False

    original = content

    # --- Step 1: Replace read-only URI patterns ---
    # Pattern: sqlite3.connect(f"file:{EXPR}?mode=ro", uri=True, timeout=X)
    # or sqlite3.connect(f"file:{EXPR}?mode=ro", uri=True)
    # Need to handle nested braces inside f-strings carefully.
    # We'll use a non-greedy approach.

    # Match f"file:...?mode=ro" with optional timeout
    content = re.sub(
        r'sqlite3\.connect\(f"file:\{([^}]+)\}\?mode=ro",\s*uri=True(?:,\s*timeout=(\d+))?\)',
        lambda m: f'get_readonly_sqlite_connection({m.group(1)}{", timeout=" + m.group(2) if m.group(2) else ""})',
        content
    )

    # --- Step 2: Replace regular sqlite3.connect(...) calls ---
    # Match: sqlite3.connect(EXPR, timeout=X) or sqlite3.connect(EXPR)
    # The EXPR can contain dots, colons, underscores, string literals, etc.
    # But NOT nested parentheses.
    content = re.sub(
        r'sqlite3\.connect\(([^()]+)\)',
        lambda m: f'get_sqlite_connection({m.group(1)})',
        content
    )

    if content == original:
        return False

    lines = content.split('\n')

    # --- Step 3: Add or update import ---
    has_existing_import = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if 'from .sqlite_utils import' in stripped or 'from core.sqlite_utils import' in stripped:
            has_existing_import = True
            if 'get_sqlite_connection' not in stripped:
                # Add our functions to the existing import
                if stripped.endswith(','):
                    lines[i] = stripped + ' get_sqlite_connection, get_readonly_sqlite_connection'
                else:
                    lines[i] = stripped + ', get_sqlite_connection, get_readonly_sqlite_connection'
            elif 'get_readonly_sqlite_connection' not in stripped:
                # Add read-only function
                if stripped.endswith(','):
                    lines[i] = stripped + ' get_readonly_sqlite_connection'
                else:
                    lines[i] = stripped + ', get_readonly_sqlite_connection'
            content = '\n'.join(lines)
            break

    if not has_existing_import:
        insert_pos = _find_import_insert_line(lines)
        import_line = 'from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection'
        lines.insert(insert_pos, import_line)
        content = '\n'.join(lines)

    # --- Step 4: Clean up unused sqlite3 import (if sqlite3 is no longer used directly) ---
    # Only remove if sqlite3 is not used for anything other than connect
    has_other_sqlite3_usage = bool(re.search(r'(?<!\.)sqlite3\.(?!connect\b)\w+', content))
    if not has_other_sqlite3_usage and 'import sqlite3' in content:
        lines = content.split('\n')
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped == 'import sqlite3' or stripped.startswith('import sqlite3') or stripped.startswith('from sqlite3'):
                continue
            new_lines.append(line)
        content = '\n'.join(new_lines)

    with open(filepath, 'w') as f:
        f.write(content)

    print(f"  CONVERTED: {relpath}")
    return True


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    core_dir = os.path.join(root, 'core')
    print("Scanning core/ for files with sqlite3.connect()...")

    converted = 0
    for dirpath, dirnames, filenames in os.walk(core_dir):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in sorted(filenames):
            if fn.endswith('.py'):
                fp = os.path.join(dirpath, fn)
                if convert_file(fp):
                    converted += 1

    print(f"\nConverted {converted} files.")


if __name__ == '__main__':
    main()