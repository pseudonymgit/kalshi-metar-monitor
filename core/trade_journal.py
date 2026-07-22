#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 4.4: Adaptive confidence thresholds - rolling 30d accuracy-based threshold adjustment]
# 2. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
# 3. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

"""
Trade Journal v1.0 — Phase 3.3 Alert Decision Logging

SQLite-backed trade journal that records every alert decision with full
decision context. Provides query methods for extraction analysis, performance
auditing, and settlement tracking.

Key features:
- Append-only journal: every alert decision is recorded
- Query methods: filter by station, date, lane, outcome
- Settlement backfill: update outcome after market settlement
- Integrated with alert_builder, risk_controls, and paper_trading_engine

Schema:
    CREATE TABLE trade_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp_utc TEXT NOT NULL,
        station TEXT NOT NULL,
        market TEXT NOT NULL,
        direction TEXT NOT NULL,
        signal_ids TEXT,
        confidence REAL,
        edge REAL,
        market_prob REAL,
        lane TEXT,
        outcome TEXT,
        alert_id TEXT,
        failure_mode TEXT
    )

Scripts only — no AI/ML in the journal loop.
"""

import sqlite3
import json
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Default journal database path
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOURNAL_PATH = str(REPO_ROOT / "data" / "trade_journal.db")


# ─── Outcome Constants ───────────────────────────────────────────────────

class JournalOutcome:
    """Standardized outcomes for trade journal entries."""
    EXECUTED = "EXECUTED"               # Trade was placed
    SKIPPED_EDGE = "SKIPPED_EDGE"       # Edge below threshold
    SKIPPED_COST = "SKIPPED_COST"       # Edge below 1.5x cost
    SKIPPED_CONFIDENCE = "SKIPPED_CONFIDENCE"  # Confidence too low
    SKIPPED_FILTER = "SKIPPED_FILTER"   # Hard filter (alert_builder)
    SKIPPED_COOLDOWN = "SKIPPED_COOLDOWN"  # Cooldown active
    SKIPPED_RISK = "SKIPPED_RISK"       # Risk kill switch
    SKIPPED_STATION = "SKIPPED_STATION"  # Station not approved
    SKIPPED_SKILL = "SKIPPED_SKILL"     # Station not skilled
    SKIPPED_WINDOW = "SKIPPED_WINDOW"   # Outside entry window
    SKIPPED_CLUSTER = "SKIPPED_CLUSTER"  # Cluster budget exhausted
    SKIPPED_CITY_PAIR = "SKIPPED_CITY_PAIR"  # City pair cap reached
    SKIPPED_SIZE_ZERO = "SKIPPED_SIZE_ZERO"  # Position size = 0
    ERROR = "ERROR"                     # Unexpected error
    SETTLED_WIN = "SETTLED_WIN"         # Trade settled profitably
    SETTLED_LOSS = "SETTLED_LOSS"       # Trade settled as loss
    EXPIRED = "EXPIRED"                 # Market expired without resolution


class TradeJournal:
    """
    SQLite-backed trade journal for recording every alert decision.

    Appends on every alert decision and provides query/cursor methods
    for analysis, auditing, and reporting.
    """

    def __init__(self, db_path: str = DEFAULT_JOURNAL_PATH):
        """
        Initialize the trade journal.

        Args:
            db_path: Path to SQLite database file. Defaults to data/trade_journal.db.
        """
        self._db_path = db_path
        self._ensure_schema()
        self._logger = logging.getLogger(f"{__name__}.TradeJournal")

    def _ensure_schema(self):
        """Create the trade_journal table if it doesn't exist."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    station TEXT NOT NULL,
                    market TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    signal_ids TEXT,
                    confidence REAL,
                    edge REAL,
                    market_prob REAL,
                    lane TEXT,
                    outcome TEXT,
                    alert_id TEXT,
                    failure_mode TEXT,
                    position_size REAL,
                    trade_version TEXT,
                    functionality TEXT,
                    metadata_json TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_journal_station
                ON trade_journal(station)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_journal_timestamp
                ON trade_journal(timestamp_utc)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_journal_outcome
                ON trade_journal(outcome)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_journal_alert_id
                ON trade_journal(alert_id)
            """)
            conn.commit()
        finally:
            conn.close()

    def append_entry(
        self,
        station: str,
        market: str,
        direction: str,
        outcome: str,
        signal_ids: Optional[str] = None,
        confidence: Optional[float] = None,
        edge: Optional[float] = None,
        market_prob: Optional[float] = None,
        lane: Optional[str] = None,
        alert_id: Optional[str] = None,
        failure_mode: Optional[str] = None,
        position_size: Optional[float] = None,
        trade_version: Optional[str] = None,
        functionality: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp_utc: Optional[str] = None,
    ) -> int:
        """
        Append a journal entry for an alert decision.

        Args:
            station: Station ICAO code
            market: Market type (HIGH, LOW)
            direction: Signal direction (UP, DOWN)
            outcome: Standardized outcome string (see JournalOutcome)
            signal_ids: Comma-separated signal IDs or signal names
            confidence: Signal confidence (0.0-1.0)
            edge: Edge value (confidence - market_prob)
            market_prob: Market-implied probability (0.0-1.0)
            lane: Lane type (regular, sure_thing, goldilocks)
            alert_id: Unique alert identifier for traceability
            failure_mode: Reason for failure/skip (if applicable)
            position_size: Position size in USD (if executed)
            trade_version: Version tag for the algorithm
            functionality: Functionality description
            metadata: Additional metadata dict (will be JSON-serialized)
            timestamp_utc: Override timestamp (ISO format). Defaults to now.

        Returns:
            Row ID of the inserted entry
        """
        if timestamp_utc is None:
            timestamp_utc = datetime.now(timezone.utc).isoformat()

        metadata_json = json.dumps(metadata, sort_keys=True) if metadata else None

        conn = get_sqlite_connection(self._db_path)
        try:
            cur = conn.execute("""
                INSERT INTO trade_journal
                    (timestamp_utc, station, market, direction, signal_ids,
                     confidence, edge, market_prob, lane, outcome, alert_id,
                     failure_mode, position_size, trade_version, functionality,
                     metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp_utc, station, market, direction, signal_ids,
                confidence, edge, market_prob, lane, outcome, alert_id,
                failure_mode, position_size, trade_version, functionality,
                metadata_json,
            ))
            conn.commit()
            row_id = cur.lastrowid
            return row_id if row_id else 0
        finally:
            conn.close()

    def append_from_decision(
        self,
        station: str,
        market: str,
        direction: str,
        decision_result: Dict[str, Any],
        signal_ids: Optional[str] = None,
        lane: Optional[str] = None,
    ) -> int:
        """
        Append a journal entry from a paper_trading_engine decision result dict.

        Extracts fields from the result dict returned by place_paper_trade().

        Args:
            station: Station ICAO code
            market: Market type (HIGH, LOW)
            direction: Signal direction (UP, DOWN)
            decision_result: Dict from place_paper_trade() or similar
            signal_ids: Optional signal identifiers
            lane: Optional lane type

        Returns:
            Row ID of the inserted entry
        """
        status = decision_result.get("status", "unknown")
        confidence = decision_result.get("confidence")
        market_prob = decision_result.get("market_prob") or decision_result.get("market_price")
        analytical_prob = decision_result.get("analytical_prob")
        edge = (confidence - market_prob) if (confidence is not None and market_prob is not None) else None
        failure_mode = decision_result.get("reason") if status == "skipped" else None
        position_size = decision_result.get("position_size_usd") or decision_result.get("cost")
        trade_version = decision_result.get("trade_version")
        functionality = decision_result.get("functionality")

        # Map status to outcome
        if status == "executed":
            outcome = JournalOutcome.EXECUTED
        elif decision_result.get("risk_killed"):
            outcome = JournalOutcome.SKIPPED_RISK
        elif failure_mode:
            # Try to classify the failure mode
            failure = str(failure_mode).lower()
            if "cooldown" in failure:
                outcome = JournalOutcome.SKIPPED_COOLDOWN
            elif "edge" in failure or "insufficient" in failure:
                outcome = JournalOutcome.SKIPPED_EDGE
            elif "cost" in failure or "round-trip" in failure:
                outcome = JournalOutcome.SKIPPED_COST
            elif "station" in failure and "not" in failure:
                outcome = JournalOutcome.SKIPPED_STATION
            elif "skill" in failure:
                outcome = JournalOutcome.SKIPPED_SKILL
            elif "window" in failure:
                outcome = JournalOutcome.SKIPPED_WINDOW
            elif "cluster" in failure:
                outcome = JournalOutcome.SKIPPED_CLUSTER
            elif "city" in failure or "pair" in failure:
                outcome = JournalOutcome.SKIPPED_CITY_PAIR
            elif "position" in failure or "zero" in failure:
                outcome = JournalOutcome.SKIPPED_SIZE_ZERO
            elif "filter" in failure:
                outcome = JournalOutcome.SKIPPED_FILTER
            elif "confidence" in failure:
                outcome = JournalOutcome.SKIPPED_CONFIDENCE
            else:
                outcome = JournalOutcome.SKIPPED_FILTER
        else:
            outcome = JournalOutcome.SKIPPED_FILTER

        return self.append_entry(
            station=station,
            market=market,
            direction=direction,
            outcome=outcome.value if hasattr(outcome, 'value') else outcome,
            signal_ids=signal_ids,
            confidence=confidence,
            edge=edge,
            market_prob=market_prob,
            lane=lane,
            failure_mode=failure_mode,
            position_size=position_size,
            trade_version=trade_version,
            functionality=functionality,
            metadata={"analytical_prob": analytical_prob} if analytical_prob else None,
        )

    def update_outcome(self, alert_id: str, outcome: str, metadata: Optional[Dict[str, Any]] = None):
        """
        Update the outcome of a previously recorded entry.

        Used for settlement backfill — e.g., updating EXECUTED to SETTLED_WIN.

        Args:
            alert_id: The alert_id to update
            outcome: New outcome string
            metadata: Optional additional metadata to merge
        """
        conn = get_sqlite_connection(self._db_path)
        try:
            if metadata:
                # Read existing metadata, merge, rewrite
                row = conn.execute(
                    "SELECT metadata_json FROM trade_journal WHERE alert_id = ?",
                    (alert_id,)
                ).fetchone()
                existing = json.loads(row[0]) if row and row[0] else {}
                existing.update(metadata)
                conn.execute("""
                    UPDATE trade_journal
                    SET outcome = ?, metadata_json = ?
                    WHERE alert_id = ?
                """, (outcome, json.dumps(existing, sort_keys=True), alert_id))
            else:
                conn.execute("""
                    UPDATE trade_journal
                    SET outcome = ?
                    WHERE alert_id = ?
                """, (outcome, alert_id))
            conn.commit()
        finally:
            conn.close()

    def query(
        self,
        station: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        outcome: Optional[str] = None,
        lane: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Query journal entries with optional filters.

        Args:
            station: Filter by station (optional)
            start_date: Filter by >= timestamp (ISO format, optional)
            end_date: Filter by < timestamp (ISO format, optional)
            outcome: Filter by outcome string (optional)
            lane: Filter by lane (optional)
            limit: Max results (default 100)

        Returns:
            List of dicts with journal entry fields
        """
        conditions = []
        params = []

        if station:
            conditions.append("station = ?")
            params.append(station)
        if start_date:
            conditions.append("timestamp_utc >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("timestamp_utc < ?")
            params.append(end_date)
        if outcome:
            conditions.append("outcome = ?")
            params.append(outcome)
        if lane:
            conditions.append("lane = ?")
            params.append(lane)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        conn = get_sqlite_connection(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT * FROM trade_journal
                WHERE {where_clause}
                ORDER BY timestamp_utc DESC
                LIMIT ?
            """, (*params, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_by_alert_id(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """Get a single journal entry by alert_id."""
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trade_journal WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_stats(self) -> Dict[str, Any]:
        """
        Get aggregate statistics from the journal.

        Returns:
            Dict with counts by outcome, station, lane, etc.
        """
        conn = get_sqlite_connection(self._db_path)
        try:
            # Total entries
            total = conn.execute("SELECT COUNT(*) FROM trade_journal").fetchone()[0]

            # By outcome
            outcome_counts = dict(conn.execute("""
                SELECT outcome, COUNT(*) FROM trade_journal GROUP BY outcome
            """).fetchall())

            # By station
            station_counts = dict(conn.execute("""
                SELECT station, COUNT(*) FROM trade_journal
                GROUP BY station ORDER BY COUNT(*) DESC LIMIT 20
            """).fetchall())

            # By lane
            lane_counts = dict(conn.execute("""
                SELECT lane, COUNT(*) FROM trade_journal WHERE lane IS NOT NULL
                GROUP BY lane
            """).fetchall())

            # Date range
            date_range = conn.execute("""
                SELECT MIN(timestamp_utc), MAX(timestamp_utc) FROM trade_journal
            """).fetchone()

            # Executed trades that haven't been settled
            pending_settlement = conn.execute("""
                SELECT COUNT(*) FROM trade_journal
                WHERE outcome = 'EXECUTED'
            """).fetchone()[0]

            return {
                "total_entries": total,
                "by_outcome": outcome_counts,
                "by_station": station_counts,
                "by_lane": lane_counts,
                "date_range": {
                    "earliest": date_range[0] if date_range else None,
                    "latest": date_range[1] if date_range else None,
                },
                "pending_settlement": pending_settlement,
            }
        finally:
            conn.close()

    def get_aggregate_summary(self, days: int = 30) -> Dict[str, Any]:
        """
        Get a concise summary of journal activity for the last N days.

        Args:
            days: Number of days to look back (default 30)

        Returns:
            Dict with summary statistics
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        conn = get_sqlite_connection(self._db_path)
        try:
            # Total decisions
            total = conn.execute(
                "SELECT COUNT(*) FROM trade_journal WHERE timestamp_utc >= ?",
                (cutoff,)
            ).fetchone()[0]

            # Executed vs skipped
            executed = conn.execute(
                "SELECT COUNT(*) FROM trade_journal WHERE outcome = 'EXECUTED' AND timestamp_utc >= ?",
                (cutoff,)
            ).fetchone()[0]

            skipped = total - executed

            # Win rate from settled trades
            settled = conn.execute("""
                SELECT COUNT(*) FROM trade_journal
                WHERE outcome IN ('SETTLED_WIN', 'SETTLED_LOSS')
                AND timestamp_utc >= ?
            """, (cutoff,)).fetchone()[0]

            wins = conn.execute("""
                SELECT COUNT(*) FROM trade_journal
                WHERE outcome = 'SETTLED_WIN' AND timestamp_utc >= ?
            """, (cutoff,)).fetchone()[0]

            win_rate = wins / settled if settled > 0 else None

            # Average edge for executed trades
            avg_edge = conn.execute("""
                SELECT AVG(edge) FROM trade_journal
                WHERE outcome = 'EXECUTED' AND edge IS NOT NULL
                AND timestamp_utc >= ?
            """, (cutoff,)).fetchone()[0]

            return {
                "period_days": days,
                "total_decisions": total,
                "executed": executed,
                "skipped": skipped,
                "settled": settled,
                "wins": wins,
                "win_rate": win_rate,
                "avg_edge": avg_edge,
            }
        finally:
            conn.close()

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get the most recent trades from the journal (limit=50 by requirement)."""
        conn = get_sqlite_connection(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''SELECT * FROM trade_journal  
                ORDER BY timestamp_utc DESC
                LIMIT ?''', (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    
    def get_recent_entries(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get the most recent journal entries."""
        return self.query(limit=limit)

    def get_accuracy_by_signal(self) -> Dict[str, Dict[str, Any]]:
        """Get accuracy statistics grouped by signal type."""
        conn = get_sqlite_connection(self._db_path)
        try:
            # Get success rates by functionality (signal type)
            rows = conn.execute('''
                SELECT 
                    functionality,
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'EXECUTED' THEN 1 ELSE 0 END) as placed,
                    SUM(CASE WHEN outcome = 'SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'SETTLED_LOSS' THEN 1 ELSE 0 END) as losses
                FROM trade_journal 
                WHERE outcome IN ('EXECUTED', 'SETTLED_WIN', 'SETTLED_LOSS', 'SKIPPED_EDGE', 'SKIPPED_FILTER')
                GROUP BY functionality
                HAVING total > 0
                ORDER BY total DESC
            ''').fetchall()
            
            results = {}
            for row in rows:
                func = row[0] if row[0] else 'unknown'
                total = row[1] or 0
                wins = row[3] or 0
                losses = row[4] or 0
                placed = row[2] or 0 
                
                accuracy = (wins / placed * 100.0) if placed > 0 else 0.0
                win_rate = (wins / (wins + losses) * 100.0) if (wins + losses) > 0 else 0.0
                
                results[func] = {
                    'total': total,
                    'placed': placed,
                    'executed': placed,  # Same as placed for our purposes
                    'wins': wins, 
                    'losses': losses,
                    'directional_accuracy_pct': accuracy,
                    'win_rate_pct': win_rate,
                    'success_ratio': f'{wins}/{wins + losses}' if (wins + losses) > 0 else '0/0'
                }
            return results
        finally:
            conn.close()
    
    def get_accuracy_by_signal_station(self, window_days: int = 30) -> Dict[str, Dict[str, Dict[str, float]]]:
        """
        Get accuracy statistics by signal type and station with configurable window.
        
        Args:
            window_days: Number of days to look back (default 30)
            
        Returns:
            Dict keyed as {signal_name: {station: {metric: value}}}
            Example: {"late_day_momentum": {"KATL": {"accuracy": 0.72, "win_rate": 0.65}}}
        """
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        
        conn = get_sqlite_connection(self._db_path)
        try:
            # Get success rates by functionality (signal type) and station
            rows = conn.execute('''
                SELECT 
                    functionality,
                    station,
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome = 'EXECUTED' THEN 1 ELSE 0 END) as placed,
                    SUM(CASE WHEN outcome = 'SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'SETTLED_LOSS' THEN 1 ELSE 0 END) as losses
                FROM trade_journal 
                WHERE outcome IN ('EXECUTED', 'SETTLED_WIN', 'SETTLED_LOSS', 'SKIPPED_EDGE', 'SKIPPED_FILTER')
                AND timestamp_utc >= ?
                GROUP BY functionality, station
                HAVING total > 0
                ORDER BY total DESC
            ''', (cutoff_date,)).fetchall()
            
            results = {}
            for row in rows:
                func = row[0] if row[0] else 'unknown'
                station = row[1] if row[1] else 'unknown'
                total = row[2] or 0
                placed = row[3] or 0
                wins = row[4] or 0
                losses = row[5] or 0

                # Calculate metrics
                accuracy = (wins / placed * 100.0) if placed > 0 else 0.0
                win_rate = (wins / (wins + losses) * 100.0) if (wins + losses) > 0 else 0.0

                if func not in results:
                    results[func] = {}
                results[func][station] = {
                    'accuracy_pct': accuracy,
                    'win_rate_pct': win_rate,
                    'total': total,
                    'placed': placed,
                    'wins': wins,
                    'losses': losses
                }
            return results
        finally:
            conn.close()

    def get_trade_counts_by_station(self) -> Dict[str, Dict[str, int]]:
        """Get trade counts (successful, skipped, total) by station."""
        conn = get_sqlite_connection(self._db_path)  
        try:
            # Get counts by station and outcome category
            rows = conn.execute('''
                SELECT 
                    station,
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome IN ('EXECUTED', 'SETTLED_WIN', 'SETTLED_LOSS') THEN 1 ELSE 0 END) as traded,
                    SUM(CASE WHEN outcome = 'SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'SETTLED_LOSS' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome IN ('SKIPPED_EDGE', 'SKIPPED_COST', 'SKIPPED_CONFIDENCE', 'SKIPPED_FILTER', 'SKIPPED_COOLDOWN', 'SKIPPED_RISK', 'SKIPPED_STATION', 'SKIPPED_SKILL', 'SKIPPED_WINDOW', 'SKIPPED_CLUSTER', 'SKIPPED_CITY_PAIR', 'SKIPPED_SIZE_ZERO', 'ERROR') THEN 1 ELSE 0 END) as skipped
                FROM trade_journal
                GROUP BY station
                ORDER BY total DESC
            ''').fetchall()
            
            results = {}
            for row in rows:
                station = row[0]
                results[station] = {
                    'total': row[1] or 0,
                    'traded': row[2] or 0,
                    'wins': row[3] or 0,
                    'losses': row[4] or 0,
                    'skipped': row[5] or 0
                }
            return results
        finally:
            conn.close()
    
    def get_failure_breakdown(self) -> List[Dict[str, Any]]:
        """Get a breakdown of skip/failure reasons."""
        conn = get_sqlite_connection(self._db_path)
        try:
            rows = conn.execute("""
                SELECT outcome, failure_mode, COUNT(*) as count
                FROM trade_journal
                WHERE outcome LIKE 'SKIP%'
                GROUP BY outcome, failure_mode
                ORDER BY count DESC
                LIMIT 20
            """).fetchall()
            return [{"outcome": r[0], "failure_mode": r[1], "count": r[2]} for r in rows]
        finally:
            conn.close()

    def close(self):
        """No-op. SQLite connections are per-operation."""
        pass


# ─── Module-level convenience singleton ──────────────────────────────────

_JOURNAL: Optional[TradeJournal] = None


def get_journal(db_path: Optional[str] = None) -> TradeJournal:
    """Get or create the module-level TradeJournal singleton."""
    global _JOURNAL
    if _JOURNAL is None:
        _JOURNAL = TradeJournal(db_path or DEFAULT_JOURNAL_PATH)
    return _JOURNAL


def record_decision(
    station: str,
    market: str,
    direction: str,
    decision_result: Dict[str, Any],
    signal_ids: Optional[str] = None,
    lane: Optional[str] = None,
) -> int:
    """Convenience: record a trade decision in the journal."""
    return get_journal().append_from_decision(
        station=station,
        market=market,
        direction=direction,
        decision_result=decision_result,
        signal_ids=signal_ids,
        lane=lane,
    )


# ─── Self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection

    # Quick test
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_path = f.name

    try:
        journal = TradeJournal(test_path)
        # Append a few entries
        e1 = journal.append_entry(
            station="KATL", market="HIGH", direction="UP",
            outcome=JournalOutcome.EXECUTED, confidence=0.75, edge=0.15,
            market_prob=0.60, lane="sure_thing", alert_id="test_001",
            trade_version="v3.0", functionality="late_day_momentum",
        )
        e2 = journal.append_entry(
            station="KLAX", market="HIGH", direction="DOWN",
            outcome=JournalOutcome.SKIPPED_EDGE, confidence=0.55, edge=0.02,
            market_prob=0.53, lane="regular", failure_mode="Edge too low",
        )
        print(f"Inserted entries: {e1}, {e2}")

        stats = journal.get_stats()
        print(f"Stats: {stats}")

        entries = journal.get_recent_entries(5)
        print(f"Recent entries: {len(entries)}")
        for e in entries:
            print(f"  {e['station']} {e['direction']} -> {e['outcome']}")

        print("Trade journal OK")
    finally:
        os.unlink(test_path)