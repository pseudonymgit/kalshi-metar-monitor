#!/usr/bin/env python3
"""A6 — om_gfs strike-crossing re-run on the CORRECT ASOS loader (2026-10-13).

The published om_gfs study (scripts/om_gfs_strike_crossing_test_2026-10-13.py,
commit 46b257344) used a bespoke `load_asos_fixed` with `ldate = ds` (no
cross-day carry). The subsequent ASOS-loader review (commit 9212a631f /
6a6832d3a) established that the "pre-dawn cross-day bug" the bespoke loader was
avoiding does NOT exist in the diversity study's `PDS.load_asos`; the bespoke
`ldate = ds` loader is the one that mis-dates ~22.6% of ASOS rows
(local-evening rows folded into the next UTC day).

The published DO_NOT_WIRE verdict was shown to be safe (hsf_at_pin is
byte-identical between the two loaders on the study window: 0 diffs), but the
published *numbers* should be regenerated on the correct loader before being
cited again.

This driver imports the study module unmodified and swaps ONLY the loader:
    OM.load_asos_fixed = PDS.load_asos
then re-runs the full phase ('all': collect + analyze) and writes the regen
artifacts to a SEPARATE directory so the published originals are preserved.

Usage:
    python3 scripts/om_gfs_strike_crossing_rerun_correct_loader_2026-10-13.py
"""
import importlib.util as _ilu
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PDS = _load("predictor_diversity_study",
            REPO / "scripts" / "predictor_diversity_study_2026-10-13.py")
OM = _load("om_gfs_strike_crossing_test",
           REPO / "scripts" / "om_gfs_strike_crossing_test_2026-10-13.py")

# ── the one change under test: correct local-date loader ─────────────────
assert OM.load_asos_fixed is not PDS.load_asos, "loader swap did not take"
OM.load_asos_fixed = PDS.load_asos

# ── write regen artifacts to a separate dir; keep originals untouched ────
OM.ART = REPO / "artifacts" / "om-gfs-strike-crossing-regen"

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "all", "--keep-cache"]
    OM.main()
    # rename to distinct regen filenames so both versions coexist
    for stem, suffix in (
            ("om_gfs_strike_crossing_2026-10-13", "json"),
            ("om_gfs_strike_crossing_2026-10-13", "md")):
        src = OM.ART / f"{stem}.{suffix}"
        dst = OM.ART / f"om_gfs_strike_crossing_regen_correct_loader_2026-10-13.{suffix}"
        if src.exists():
            dst.write_bytes(src.read_bytes())
            src.unlink()
    print(f"[regen] artifacts in {OM.ART}")