"""
structured_logger.py — Structured JSON Logging for Weather Engine

Replaces ad-hoc print() calls with structured JSON log entries.
All modules should import and use get_logger() instead of print().

Example:
    from core.structured_logger import get_logger
    log = get_logger("metar_monitor")
    log.info("Data collected", event="metar_poll", station="KDEN", temp_f=72.5)

Log output format (one JSON object per line):
    {"timestamp": "2026-07-22T03:30:00.123Z", "level": "INFO", "module": "metar_monitor",
     "event_id": "metar_poll", "message": "Data collected", "station": "KDEN", "temp_f": 72.5}
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class StructuredFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") +
                         f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z",
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # Include event_id if provided as first arg via extra
        if hasattr(record, "event_id") and record.event_id:
            log_entry["event_id"] = record.event_id

        # Include any extra fields from the 'extra' dict
        for key in dir(record):
            if key.startswith("_") or key in ("args", "msg", "name", "levelname", "levelno",
                                               "pathname", "filename", "module", "exc_info",
                                               "exc_text", "stack_info", "lineno", "funcName",
                                               "created", "msecs", "relativeCreated", "thread",
                                               "threadName", "process", "processName", "message"):
                continue
            val = getattr(record, key, None)
            if val is not None and not callable(val):
                log_entry[key] = val

        # Include exception info if present
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
            }

        return json.dumps(log_entry, default=str)


_LOG_LEVEL_ENV_KEY = "LOG_LEVEL"
_DEFAULT_LOG_LEVEL = logging.INFO

# Root structured logger
_root_configured = False
_root_lock = threading.Lock()


def configure_root_logger(level: Optional[str] = None) -> None:
    """Configure the root logger for structured JSON output once.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR). Defaults to LOG_LEVEL env var or INFO.
    """
    global _root_configured
    with _root_lock:
        if _root_configured:
            return

        log_level_str = level or os.environ.get(_LOG_LEVEL_ENV_KEY, "INFO")
        numeric_level = getattr(logging, log_level_str.upper(), _DEFAULT_LOG_LEVEL)

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())

        root = logging.getLogger()
        root.setLevel(numeric_level)
        # Remove default handlers to avoid duplicate output
        for h in root.handlers[:]:
            root.removeHandler(h)
        root.addHandler(handler)

        _root_configured = True


def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    """Get a structured JSON logger for the given module name.

    Args:
        name: Module name (e.g., 'metar_monitor', 'alert_builder')
        level: Optional override log level

    Returns:
        Configured logger instance
    """
    configure_root_logger()
    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)
    return logger


# Convenience module-level logger for this module
log = get_logger("structured_logger")


class LogContext:
    """Context manager for adding structured context to all log calls within a block.

    Usage:
        with LogContext(module="metar_monitor", station="KDEN"):
            log.info("Processing", event_id="process_start")
    """

    def __init__(self, **context: Any):
        self._context = context
        self._original_factory = None

    def __enter__(self):
        # We store context in thread-local storage
        self._original_factory = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = self._original_factory(*args, **kwargs)
            for key, val in self._context.items():
                setattr(record, key, val)
            return record

        logging.setLogRecordFactory(factory)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logging.setLogRecordFactory(self._original_factory)


# ─── Legacy bridging: Provide a simple print-replacement ──────────────────

def log_print(*args, level: str = "INFO", event_id: Optional[str] = None, **kwargs) -> None:
    """Replacement for print() that emits structured JSON.

    Usage in migration:
        from core.structured_logger import log_print
        log_print("METAR refresh complete", event_id="startup")
        # Equivalent old: print("[startup] METAR refresh complete")
    """
    configure_root_logger()
    message = " ".join(str(a) for a in args)
    extra = {"event_id": event_id} if event_id else {}
    extra.update(kwargs)

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger("legacy").log(numeric_level, message, extra=extra)
