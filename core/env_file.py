"""env_file — minimal, dependency-free loader for repo ``.env`` files.

WHY
---
WE crons run via ``sh -lc <cmd>`` with no shell-init that sources ``.env``.
As a result, keys like ``WEBHOOK_SBOX`` never reach the process environment,
so the SBOX bridge's trade-failure notify path silently no-ops (dispatch
resolves no webhook URL and returns early). This loader bridges that gap by
reading an ``.env``-style file into ``os.environ``.

DESIGN
------
- **No-clobber by default**: a real environment variable always wins over the
  file. Pass ``override=True`` only in tests.
- **Tolerant**: skips comments, blank lines, and malformed lines (e.g. a stray
  ``-e`` token) — it never executes file content, never raises.
- **Deterministic**: pure parse of ``KEY=VALUE`` lines; strips one layer of
  matching surrounding quotes.
"""

import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["load_env_file", "parse_env_text"]

_REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse ``.env``-style text into a dict. Never raises.

    Rules: ignore blank lines and ``#`` comments; ignore lines without ``=``;
    strip one layer of matching single/double quotes; skip keys that are not
    alphanumeric/underscore.
    """
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue  # malformed (e.g. stray token) — skip, do not execute
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if not key or not key.replace("_", "").isalnum():
            continue
        out[key] = val
    return out


def load_env_file(path: Optional[str] = None, override: bool = False) -> int:
    """Load ``path`` (default: repo-root ``.env``) into ``os.environ``.

    Returns the number of variables set. Missing/unreadable file is a no-op.
    """
    p = Path(path) if path else (_REPO_ROOT / ".env")
    if not p.exists():
        logger.debug("env file not found: %s", p)
        return 0
    try:
        data = parse_env_text(p.read_text(encoding="utf-8"))
    except OSError as e:  # unreadable / permission — never fatal
        logger.warning("could not read env file %s: %s", p, e)
        return 0
    n = 0
    for k, v in data.items():
        if override or k not in os.environ:
            os.environ[k] = v
            n += 1
    return n
