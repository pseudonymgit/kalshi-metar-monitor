#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

"""
Night Mode v1.0 — Phase 4.1 Operator Availability Model

Determines whether the system is in "night mode" (off-hours) or "active hours"
based on configurable operator availability windows.

When in night mode:
  - All signals are logged to a digest queue (SQLite)
  - Real-time alerts are suppressed (not sent to Discord)
  - A morning digest is generated at 8am ET summarizing suppressed alerts
  - Critical-only override: Sure Thing confidence > 0.80 triggers immediate alert

Active hours:
  - Weekdays: 8am-8pm ET
  - Weekends: 10am-4pm ET (reduced)
  - Night mode: all other hours

Scripts only — no AI/ML in the night mode loop.
"""

import sqlite3
import json
import os
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIGEST_DB_PATH = str(REPO_ROOT / "data" / "night_mode_digest.db")

# ─── Default Availability Windows (ET) ──────────────────────────────────

# Weekday active hours (Mon-Fri): 8am - 8pm ET
WEEKDAY_ACTIVE_START_HOUR_ET = 8
WEEKDAY_ACTIVE_END_HOUR_ET = 20

# Weekend active hours (Sat-Sun): 10am - 4pm ET
WEEKEND_ACTIVE_START_HOUR_ET = 10
WEEKEND_ACTIVE_END_HOUR_ET = 16

# Digest delivery time: 8am ET
DIGEST_HOUR_ET = 8
DIGEST_MINUTE_ET = 0

# Sure Thing confidence threshold for critical override
CRITICAL_OVERRIDE_CONFIDENCE = 0.80


# ─── ET Timezone Helper ─────────────────────────────────────────────────

def _now_et() -> datetime:
    """
    Get current time in US Eastern Time.
    
    Uses a fixed UTC-5 offset (EST) during standard time and UTC-4 (EDT)
    during daylight saving time. This is a simplified approximation that
    avoids the pytz dependency.
    
    DST in US: starts 2nd Sunday March, ends 1st Sunday November
    """
    utc_now = datetime.now(timezone.utc)
    
    # Get the year
    year = utc_now.year
    
    # Compute DST start (2nd Sunday of March)
    march_1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    # Find 2nd Sunday
    days_to_first_sunday = (6 - march_1.weekday()) % 7
    dst_start = march_1 + timedelta(days=days_to_first_sunday + 7)  # 2nd Sunday
    dst_start = dst_start.replace(hour=7)  # 2am ET = 7am UTC
    
    # Compute DST end (1st Sunday of November)
    nov_1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    days_to_first_sunday = (6 - nov_1.weekday()) % 7
    dst_end = nov_1 + timedelta(days=days_to_first_sunday)  # 1st Sunday
    dst_end = dst_end.replace(hour=6)  # 2am ET = 6am UTC (back to EST)
    
    # Apply DST offset
    if dst_start <= utc_now < dst_end:
        # EDT (UTC-4)
        et_offset = timedelta(hours=-4)
    else:
        # EST (UTC-5)
        et_offset = timedelta(hours=-5)
    
    return utc_now + et_offset


def is_active_hours(dt_et: Optional[datetime] = None) -> bool:
    """
    Check if current time is within operator active hours.
    
    Args:
        dt_et: Datetime in ET timezone. If None, uses current time.
    
    Returns:
        True if within active hours, False if in night mode
    """
    if dt_et is None:
        dt_et = _now_et()
    
    hour = dt_et.hour
    weekday = dt_et.weekday()  # Monday=0, Sunday=6
    
    if weekday < 5:
        # Weekday: 8am-8pm ET
        return WEEKDAY_ACTIVE_START_HOUR_ET <= hour < WEEKDAY_ACTIVE_END_HOUR_ET
    else:
        # Weekend: 10am-4pm ET
        return WEEKEND_ACTIVE_START_HOUR_ET <= hour < WEEKEND_ACTIVE_END_HOUR_ET


def is_night_mode(dt_et: Optional[datetime] = None) -> bool:
    """
    Check if the system is in night mode (inverse of active hours).
    
    Args:
        dt_et: Datetime in ET. If None, uses current time.
    
    Returns:
        True if in night mode (off-hours)
    """
    return not is_active_hours(dt_et)


def is_digest_time(dt_et: Optional[datetime] = None, tolerance_minutes: int = 5) -> bool:
    """
    Check if current time is within the morning digest delivery window.
    
    Args:
        dt_et: Datetime in ET. If None, uses current time.
        tolerance_minutes: How many minutes past digest time to accept
    
    Returns:
        True if within the digest delivery window
    """
    if dt_et is None:
        dt_et = _now_et()
    
    digest_start = DIGEST_HOUR_ET * 60 + DIGEST_MINUTE_ET
    current_minutes = dt_et.hour * 60 + dt_et.minute
    
    return digest_start <= current_minutes < digest_start + tolerance_minutes


# ─── Digest Queue ───────────────────────────────────────────────────────

class NightModeDigest:
    """
    Manages the night mode digest queue.
    
    During night mode, signals are queued instead of sending real-time alerts.
    At 8am ET, a morning digest is generated and sent.
    
    Critical-only override: if confidence > 0.80 (Sure Thing), the alert
    is sent immediately even during night mode.
    """
    
    def __init__(self, db_path: str = DEFAULT_DIGEST_DB_PATH):
        """
        Args:
            db_path: Path to SQLite database for the digest queue.
        """
        self._db_path = db_path
        self._ensure_schema()
        self._logger = logging.getLogger(f"{__name__}.NightModeDigest")
    
    def _ensure_schema(self):
        """Create the digest_queue table."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS digest_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    station TEXT NOT NULL,
                    market TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    confidence REAL,
                    edge REAL,
                    lane TEXT,
                    sent TINYINT DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_digest_sent
                ON digest_queue(sent)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_digest_timestamp
                ON digest_queue(timestamp_utc)
            """)
            conn.commit()
        finally:
            conn.close()
    
    def queue_signal(self, station: str, market: str, direction: str,
                     confidence: float, edge: float, lane: str) -> int:
        """
        Queue a signal during night mode for morning digest.
        
        Args:
            station: Station ICAO code
            market: Market type (HIGH, LOW)
            direction: Signal direction (UP, DOWN)
            confidence: Trade confidence (0.0-1.0)
            edge: Edge value (trade_conf - market_prob)
            lane: Lane type (regular, sure_thing, goldilocks)
        
        Returns:
            Queue entry ID
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.execute("""
                INSERT INTO digest_queue
                    (timestamp_utc, station, market, direction, confidence, edge, lane, sent)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """, (now_iso, station, market, direction, confidence, edge, lane))
            conn.commit()
            entry_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._logger.info(
                "Digest queued: %s/%s/%s (conf=%.2f, edge=%.2f, lane=%s) [id=%d]",
                station, market, direction, confidence, edge, lane, entry_id
            )
            return entry_id
        finally:
            conn.close()
    
    def is_critical_override(self, confidence: float, lane: str) -> bool:
        """
        Check if a signal should bypass night mode (critical override).
        
        Critical override triggers when:
        - Lane is 'sure_thing' AND confidence > 0.80
        
        Args:
            confidence: Trade confidence (0.0-1.0)
            lane: Lane type
        
        Returns:
            True if this signal should be sent immediately
        """
        return lane.lower() == 'sure_thing' and confidence >= CRITICAL_OVERRIDE_CONFIDENCE
    
    def get_pending_digest(self) -> List[Dict[str, Any]]:
        """
        Get all pending (unsent) digest entries, ordered by most significant first.
        
        Most significant = highest confidence, then highest edge.
        
        Returns:
            List of pending digest entries
        """
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM digest_queue
                WHERE sent = 0
                ORDER BY confidence DESC, edge DESC
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    
    def mark_digest_sent(self, entry_ids: List[int]) -> int:
        """
        Mark digest entries as sent (after digest delivery).
        
        Args:
            entry_ids: List of entry IDs to mark as sent
        
        Returns:
            Number of entries marked
        """
        if not entry_ids:
            return 0
        
        conn = get_sqlite_connection(self._db_path)
        try:
            placeholders = ','.join('?' * len(entry_ids))
            conn.execute(f"""
                UPDATE digest_queue
                SET sent = 1
                WHERE id IN ({placeholders})
            """, entry_ids)
            conn.commit()
            count = conn.execute(
                "SELECT changes()"
            ).fetchone()[0]
            return count or 0
        finally:
            conn.close()
    
    def build_morning_digest_message(self) -> Dict[str, Any]:
        """
        Build the morning digest message with all pending entries.
        
        Format:
          Summary: "N alerts suppressed during night mode"
          Table: station, market, direction, confidence, edge, lane
          Most significant alert at top
        
        Returns:
            Dict with digest content suitable for Discord embed
        """
        pending = self.get_pending_digest()
        
        if not pending:
            return {
                "content": None,
                "embeds": [{
                    "title": "🌅 Morning Digest — No Night Alerts",
                    "description": "No signals were suppressed during night mode.",
                    "color": 0x00FF00,
                    "footer": {"text": "Weather Engine Night Mode"},
                }],
                "digest_entries": [],
                "total_suppressed": 0,
            }
        
        # Build summary line
        total = len(pending)
        
        # Build table entries
        embed_fields = []
        for i, entry in enumerate(pending[:10]):  # Show top 10 in embed
            conf_pct = f"{entry['confidence'] * 100:.0f}%" if entry['confidence'] else "N/A"
            edge_val = f"{entry['edge']:+.2%}" if entry['edge'] is not None else "N/A"
            field_value = (
                f"Direction: {entry['direction']} | "
                f"Conf: {conf_pct} | "
                f"Edge: {edge_val} | "
                f"Lane: {entry['lane'] or 'regular'}"
            )
            embed_fields.append({
                "name": f"{i+1}. {entry['station']} — {entry['market']}",
                "value": field_value,
                "inline": False,
            })
        
        # If more than 10, add a note
        if total > 10:
            embed_fields.append({
                "name": f"... and {total - 10} more",
                "value": "See digest database for full list.",
                "inline": False,
            })
        
        # Determine significance
        top_entry = pending[0] if pending else None
        top_conf = f"{top_entry['confidence'] * 100:.0f}%" if top_entry and top_entry['confidence'] else "N/A"
        summary_detail = f"Most significant: {top_entry['station']}/{top_entry['market']} ({top_conf})" if top_entry else ""
        
        embed = {
            "title": f"🌅 Morning Digest — {total} Alert{'s' if total != 1 else ''} Suppressed",
            "description": (
                f"**{total} alert{'s' if total != 1 else ''} were suppressed during night mode.**\n"
                f"{summary_detail}"
            ),
            "color": 0xFFA500,  # Orange
            "fields": embed_fields,
            "footer": {"text": f"Weather Engine Night Mode | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
        }
        
        # Build the full digest entries list for logging/console output
        digest_entries = []
        for entry in pending:
            digest_entries.append({
                "station": entry['station'],
                "market": entry['market'],
                "direction": entry['direction'],
                "confidence": entry['confidence'],
                "edge": entry['edge'],
                "lane": entry['lane'],
            })
        
        return {
            "content": None,
            "embeds": [embed],
            "digest_entries": digest_entries,
            "total_suppressed": total,
        }
    
    def clear_old_entries(self, days: int = 30) -> int:
        """
        Clear sent entries older than specified days.
        
        Args:
            days: Delete entries older than this many days
        
        Returns:
            Number of entries deleted
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.execute("""
                DELETE FROM digest_queue
                WHERE sent = 1 AND timestamp_utc < ?
            """, (cutoff,))
            conn.commit()
            count = conn.execute("SELECT changes()").fetchone()[0]
            self._logger.info("Cleared %d old digest entries (%d+ days)", count or 0, days)
            return count or 0
        finally:
            conn.close()
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get digest queue statistics.
        
        Returns:
            Dict with pending count, total entries, etc.
        """
        conn = get_sqlite_connection(self._db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM digest_queue").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM digest_queue WHERE sent = 0").fetchone()[0]
            sent = conn.execute("SELECT COUNT(*) FROM digest_queue WHERE sent = 1").fetchone()[0]
            return {
                "total_entries": total,
                "pending": pending,
                "sent": sent,
            }
        finally:
            conn.close()


# ─── Module-level convenience functions ──────────────────────────────────

_DIGEST: Optional[NightModeDigest] = None


def get_digest(db_path: Optional[str] = None) -> NightModeDigest:
    """Get or create the module-level NightModeDigest singleton."""
    global _DIGEST
    if _DIGEST is None:
        _DIGEST = NightModeDigest(db_path or DEFAULT_DIGEST_DB_PATH)
    return _DIGEST


def queue_signal_for_night_mode(station: str, market: str, direction: str,
                                 confidence: float, edge: float, lane: str) -> int:
    """Convenience: queue a signal during night mode."""
    return get_digest().queue_signal(station, market, direction, confidence, edge, lane)


def build_morning_digest() -> Dict[str, Any]:
    """Convenience: build the morning digest message."""
    return get_digest().build_morning_digest_message()


# ─── Self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
    
    # Test timezone helper
    now_et = _now_et()
    print(f"Current ET time: {now_et.strftime('%Y-%m-%d %H:%M')}")
    print(f"Active hours: {is_active_hours()}")
    print(f"Night mode: {is_night_mode()}")
    print(f"Digest time: {is_digest_time()}")
    
    # Test digest queue
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_path = f.name
    
    try:
        digest = NightModeDigest(test_path)
        
        # Queue some test signals
        digest.queue_signal("KATL", "HIGH", "UP", 0.75, 0.12, "sure_thing")
        digest.queue_signal("KLAX", "LOW", "DOWN", 0.65, 0.08, "regular")
        digest.queue_signal("KBOS", "HIGH", "UP", 0.55, 0.05, "regular")
        
        # Check pending
        pending = digest.get_pending_digest()
        print(f"\nPending entries: {len(pending)}")
        for p in pending:
            print(f"  {p['station']} {p['market']} {p['direction']} (conf={p['confidence']}, edge={p['edge']})")
        
        # Build digest message
        msg = digest.build_morning_digest_message()
        print(f"\nDigest total suppressed: {msg['total_suppressed']}")
        print(f"Digest embed title: {msg['embeds'][0]['title']}")
        
        # Mark as sent
        ids = [p['id'] for p in pending]
        marked = digest.mark_digest_sent(ids)
        print(f"\nMarked {marked} entries as sent")
        
        stats = digest.get_stats()
        print(f"Stats: {stats}")
        
        # Test critical override
        assert digest.is_critical_override(0.85, "sure_thing") == True
        assert digest.is_critical_override(0.75, "sure_thing") == False
        assert digest.is_critical_override(0.85, "regular") == False
        print("\nCritical override tests passed")
        
        print("\nNight mode module OK")
    finally:
        os.unlink(test_path)