#!/usr/bin/env python3
"""Proof: run the SBOX EV bridge at an in-window clock (15:00 UTC) and show
executed + skip reasons. One-off diagnostic.

FOOTGUN FIXED (2026-10-10): this script used to call ``run(dry_run=False)`` and
therefore wrote the LIVE ``data/paper_trading_sbox.db`` book — a "proof" script
that mutated real state. It now defaults to DRY-RUN ONLY. A live exercise
(``--live``) is redirected to a throwaway **copy** of the book (``b.SBOX_DB`` is
pointed at a temp file), so exercising this script can never touch the real book
again. Nothing here gates trading; it is a diagnostic.
"""
import argparse
import shutil
import sys
import tempfile
import datetime as _dt
from pathlib import Path
from datetime import timezone

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")


class FakeDT(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _dt.datetime(2026, 9, 29, 15, 0, 0, tzinfo=timezone.utc)


import scripts.sbox_intraday_ev_bridge as b  # noqa: E402
b.datetime = FakeDT


def _run(dry: bool, tmpdir: Path):
    """Run the bridge; for a live exercise, point SBOX_DB at a temp COPY."""
    if dry:
        return b.run(dry_run=True, ignore_window=False, min_edge=0.0)
    # --live: never write the real book — copy it and redirect the module target.
    real = Path(b.SBOX_DB)
    copy = tmpdir / "paper_trading_sbox.proof-copy.db"
    shutil.copy2(real, copy) if real.exists() else None
    original = b.SBOX_DB
    b.SBOX_DB = copy
    try:
        out = b.run(dry_run=False, ignore_window=False, min_edge=0.0)
    finally:
        b.SBOX_DB = original
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="exercise the live path against a temp COPY of the book "
                         "(never the real book). Default: dry-run only.")
    args = ap.parse_args(argv)

    modes = [(True, "DRY")]
    if args.live:
        modes.append((False, "LIVE(copy)"))

    from collections import Counter
    with tempfile.TemporaryDirectory() as td:
        for dry, tag in modes:
            out = _run(dry, Path(td))
            print(f"{tag} n_executed={out.get('n_executed')} "
                  f"n_skipped={out.get('n_skipped')}")
            for e in out.get("executed", [])[:8]:
                print("   EXEC", e)
            print("   skips:", Counter(r for _, r in out.get("skipped", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
