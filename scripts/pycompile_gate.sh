#!/bin/bash
# WAVE-D/15 — py_compile CI gate.
# Byte-compiles every WE source file so a syntax error (like the METAR collector
# crash) is caught before deploy instead of at runtime.
# Exits non-zero if any file fails to compile.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

PY=".venv-gefs/bin/python"
[ -x "$PY" ] || PY="python3"

fail=0
n=0
while IFS= read -r f; do
  n=$((n+1))
  if ! "$PY" -m py_compile "$f" 2>/tmp/pycompile_err.txt; then
    echo "COMPILE FAIL: $f"
    cat /tmp/pycompile_err.txt
    fail=$((fail+1))
  fi
done < <(find core scripts tests -name "*.py" -not -path "*/__pycache__/*" 2>/dev/null)

echo "py_compile gate: $n files, $fail failures"
[ "$fail" -eq 0 ] && echo "PYCOMPILE: PASS" || echo "PYCOMPILE: FAIL"
exit "$fail"