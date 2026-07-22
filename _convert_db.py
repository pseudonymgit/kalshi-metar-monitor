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
    last_import = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        if stripped.startswith('from __future__'):
            continue
        if stripped.startswith('# shebang') or stripped.startswith('#!'):
            continue
        if stripped.startswith('import ') or stripped.startswith('from '):
            last_import = i
    return last_import + 1


def convert_file(filepath):
    relpath = os.path.relpath(filepath)
    if relpath in SKIP_FILES:
        print(f"  SKIP (excluded): {relpath}")
        return False

    with open(filepath, 'r') as f:
        content = f.read()

    if 'sqlite3.connect' not in content:
        return False

    original = content

    # Step 1: Replace read-only URI patterns (f-string)
    ro_pattern = re.compile(
        r'sqlite3\.connect\(f"file:\{([^}]+)\}\?mode=ro",\s*uri=True(?:,\s*timeout=(\d+))?\)'
    )
    def ro_replace(m):
        expr = m.group(1)
        timeout = m.group(2)
        if timeout:
            return f'get_readonly_sqlite_connection({expr}, timeout={timeout})'
        return f'get_readonly_sqlite_connection({expr})'

    content = ro_pattern.sub(ro_replace, content)

    # Step 2: Replace regular sqlite3.connect(...) calls (handles one level of nesting)
    reg_pattern = re.compile(r"sqlite3\.connect\(((?:[^()]|\([^()]*\))+)\)")
    content = reg_pattern.sub(r'get_sqlite_connection(\1)', content)

    if content == original:
        return False

    lines = content.split('\n')

    # Step 3: Add or update import
    has_existing_import = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if 'from .sqlite_utils import' in stripped or 'from core.sqlite_utils import' in stripped:
            has_existing_import = True
            need_update = False
            if 'get_sqlite_connection' not in stripped:
                stripped = stripped.rstrip(',') + ', get_sqlite_connection, get_readonly_sqlite_connection'
                need_update = True
            elif 'get_readonly_sqlite_connection' not in stripped:
                stripped = stripped.rstrip(',') + ', get_readonly_sqlite_connection'
                need_update = True
            if need_update:
                # Find indent
                indent = ' ' * (len(line) - len(line.lstrip()))
                lines[i] = indent + stripped
                content = '\n'.join(lines)
            break

    if not has_existing_import:
        insert_pos = _find_import_insert_line(lines)
        import_line = 'from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection'
        lines.insert(insert_pos, import_line)
        content = '\n'.join(lines)

    # Step 4: Keep sqlite3 import if still needed for other uses
    has_other_sqlite3_usage = bool(re.search(r'(?<!\.)sqlite3\.(?!connect\b)\w+', content))
    if not has_other_sqlite3_usage:
        # Remove 'import sqlite3' lines
        lines = content.split('\n')
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped == 'import sqlite3' or stripped.startswith('import sqlite3 as') or stripped.startswith('from sqlite3 import'):
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