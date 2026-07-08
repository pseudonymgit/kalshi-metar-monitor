#!/usr/bin/env python3
"""Insert Render Disk + Auto-Refresh block into app.py (idempotent)."""

import pathlib

APP = pathlib.Path("app.py")
BLOCK = """# === Render Disk + Auto-Refresh (Phase 1) ===
import os
import subprocess
import threading
import time

DATA_DIR = "/opt/render/project/src/data"
METAR_DB = os.path.join(DATA_DIR, "metar_backfill.db")

def _refresh_metar_if_stale(max_age_hours: int = 2) -> None:
    if not os.path.exists(METAR_DB):
        print("[startup] metar_backfill.db missing → running live collection")
    else:
        age_h = (time.time() - os.path.getmtime(METAR_DB)) / 3600
        if age_h < max_age_hours:
            print(f"[startup] metar_backfill.db is {age_h:.1f}h old → fresh enough")
            return
        print(f"[startup] metar_backfill.db is {age_h:.1f}h old → refreshing")

    try:
        subprocess.run(
            ["python3", "scripts/metar_collect_live.py"],
            cwd="/opt/render/project/src",
            check=True,
            timeout=300
        )
        print("[startup] METAR refresh complete")
    except Exception as e:
        print(f"[startup] METAR refresh failed: {e}")

def _start_periodic_refresh():
    def loop():
        while True:
            time.sleep(45 * 60)
            _refresh_metar_if_stale(max_age_hours=1)
    t = threading.Thread(target=loop, daemon=True)
    t.start()

if os.getenv("RENDER") == "true":
    _refresh_metar_if_stale(max_age_hours=2)
    _start_periodic_refresh()
# === End Render Disk + Auto-Refresh ===
"""

text = APP.read_text(encoding="utf-8")

if "# === Render Disk + Auto-Refresh (Phase 1) ===" in text:
    print("Block already present — nothing to do.")
else:
    # Insert after the sys.path.insert line
    marker = 'sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))'
    if marker not in text:
        print(f"ERROR: Could not find insertion point: {marker}")
        exit(1)
    new_text = text.replace(marker, marker + "\n\n" + BLOCK, 1)
    APP.write_text(new_text, encoding="utf-8")
    print("Block inserted successfully.")
