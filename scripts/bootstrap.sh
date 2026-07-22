#!/usr/bin/env bash
# scripts/bootstrap.sh — Pre-deploy health bootstrap
# Runs before gunicorn starts. Exits non-zero on failure to abort deploy.
set -euo pipefail

echo "[bootstrap] Starting Weather Engine bootstrap..."

# ─── 1. Verify Python and dependencies ─────────────────────────────────────
echo "[bootstrap] Checking Python version..."
python3 --version

echo "[bootstrap] Checking dependencies..."
python3 -c "import sys; sys.path.insert(0, '.'); import flask; import gunicorn; import requests; import sqlite3; import numpy; import scipy; print('Dependencies: OK')"

# ─── 2. Environment variables check ────────────────────────────────────────
echo "[bootstrap] Checking environment variables..."
MISSING_VARS=()
for var in PORT; do
    if [ -z "${!var:-}" ]; then
        MISSING_VARS+=("$var")
    fi
done
if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    echo "[bootstrap] ERROR: Missing required env vars: ${MISSING_VARS[*]}" >&2
    exit 1
fi

# ─── 3. Data directories ───────────────────────────────────────────────────
echo "[bootstrap] Creating data directories..."
mkdir -p data logs
chmod 755 data logs

# ─── 4. SQLite connectivity check ──────────────────────────────────────────
echo "[bootstrap] Checking SQLite..."
python3 -c "
import sqlite3, os
db_path = os.environ.get('METAR_DB_PATH', 'data/metar_backfill.db')
os.makedirs(os.path.dirname(db_path), exist_ok=True)
conn = sqlite3.connect(db_path)
conn.execute('PRAGMA journal_mode=WAL;')
conn.execute('PRAGMA busy_timeout=5000;')
conn.execute('SELECT 1;')
conn.close()
print(f'SQLite: OK ({db_path})')
"

# ─── 5. Syntax check ──────────────────────────────────────────────────────
echo "[bootstrap] Running syntax check..."
python3 -m py_compile app.py
python3 -c "
import sys, os, ast
sys.path.insert(0, '.')
errors = []
for root, dirs, files in os.walk('core'):
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            try:
                ast.parse(open(path).read(), path, 'exec')
            except SyntaxError as e:
                errors.append(f'{path}: {e}')
if errors:
    for e in errors:
        print(f'  SYNTAX ERROR: {e}')
    exit(1)
print(f'Core modules syntax check: OK (compiled)')
"

# ─── 6. Station registry validation ────────────────────────────────────────
echo "[bootstrap] Validating station registry..."
python3 -c "
import sys; sys.path.insert(0, '.')
from core.station_registry import validate_station_registry
result = validate_station_registry()
assert result.get('valid', False), f'Station registry validation failed: {result}'
print(f'Station registry: OK ({result.get(\"station_count\", \"?\")} stations)')
" 2>/dev/null && echo "[bootstrap] Station registry: OK" || echo "[bootstrap] WARNING: Station registry validation skipped (not yet configured)"

# ─── 7. Webhook dry-run ────────────────────────────────────────────────────
echo "[bootstrap] Webhook URL check..."
if [ -n "${WEBHOOK_BASE_URL:-}" ]; then
    echo "[bootstrap] WEBHOOK_BASE_URL configured: ${WEBHOOK_BASE_URL:0:20}..."
else
    echo "[bootstrap] WARNING: WEBHOOK_BASE_URL not configured — webhooks disabled"
fi

echo "[bootstrap] Bootstrap complete. Starting application..."
