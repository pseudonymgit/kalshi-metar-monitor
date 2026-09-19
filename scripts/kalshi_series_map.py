"""
kalshi_series_map.py — Canonical city -> station ICAO -> series ticker resolver.

Author: Gilfoyle (subagent), 2026-09-18.
Purpose: Single source of truth for mapping Kalshi daily-temperature series
tickers (KXHIGH*/KXLOW*) to canonical station ICAOs, handling the FIVE
inconsistent naming conventions that previously produced the FALSE "retired
stations" conclusion.

Ground truth (pulled live from GET /series, 2026-09-18):
  * 14,157 total series; 104 are KXHIGH*/KXLOW* Climate/Weather series.
  * The daily-high product is listed under inconsistent ticker names:
      - KXHIGH<city>      : KXHIGHNY, KXHIGHCHI, KXHIGHAUS, KXHIGHMIA, ...
      - KXHIGHT<icao>     : KXHIGHTATL, KXHIGHTBOS, KXHIGHTDAL, ...
      - KXHIGHTH<icao>    : KXHIGHTHOU, KXHIGHTOKC, KXHIGHTPHX, ...
      - KXHIGHT<city>     : KXHIGHTNOLA
      - duplicate/legacy  : KXHIGHTSATX + KXHIGHTSDF (both San Antonio), ...
  * LOW series are equally scattered: KXLOWT<icao> (current) and KXLOW<city>
    (legacy/dead, e.g. KXLOWNY, KXLOWCHI).

ALL 20 cities have LIVE series. The prior "11 retired stations" claim in
docs/plans/KALSHI-STRIKE-BACKFILL-2026-09-18.md §3 was WRONG — it classified
KBNA/KCLT/KDAL/KDTW/KJFK/KORD/KPDX/KSLC/KTPA/PHNL as retired, but those are
simply NOT in the settlement_epochs station set (they are distinct airports
or cities we never traded). The 20 cities we DO trade all have live series.

This module loads data/kalshi_series_map.json and provides:
  * resolve_ticker(ticker) -> station ICAO or None
  * station_for_code_suffix(suffix) -> station ICAO or None
  * aliases_for(station) -> {HIGH: [...], LOW: [...]} (current + legacy)
  * live_high_aliases(station), live_low_aliases(station)
  * canonical_stations() -> sorted list of station ICAOs in the map
  * load_map() -> the raw dict

No AI/ML, no live trading, read-only. Deterministic.
"""

import json
import os
from typing import Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAP_PATH = os.path.join(_REPO_ROOT, "data", "kalshi_series_map.json")

_HIGH_PREFIXES = ("KXHIGH",)
_LOW_PREFIXES = ("KXLOW",)


def load_map() -> dict:
    with open(_MAP_PATH, "r") as f:
        return json.load(f)


def _stations(map_: dict) -> dict:
    return map_.get("stations", {})


def _suffix_variants(map_: dict) -> dict:
    return map_.get("code_suffix_normalization", {}).get("city_suffix_variants", {})


def _code_suffix(ticker: str) -> str:
    """Return the code suffix after stripping the KXHIGH/KXLOW prefix."""
    t = (ticker or "").strip().upper()
    for prefix in ("KXHIGH", "KXLOW"):
        if t.startswith(prefix):
            return t[len(prefix):]
    return t


def _normalize_suffix(code: str) -> str:
    """Normalize a code suffix by stripping ALL leading 'T's (LOW series always
    adds a 'T'; HIGH adds it only for some cities)."""
    return (code or "").upper().lstrip("T")


def station_for_code_suffix(code: str, map_: Optional[dict] = None) -> Optional[str]:
    """Map a Kalshi code suffix (e.g. 'NY', 'TNYC', 'TATL', 'AUS', 'LV') to a
    canonical station ICAO, or None.

    Two-stage resolution:
      1. Exact/normalized city-suffix lookup (handles NY->KNYC, NYC->KNYC,
         CHI->KMDW, LV->KLAS, MIN->KMSP, NOLA->KMSY, DAL->KDFW, SATX->KSAT,
         SDF->KSAT, etc.).
      2. Fallback: reverse-search the station aliases (strip 'T' and match
         against every alias/legacy/low alias code suffix).
    """
    if map_ is None:
        map_ = load_map()
    key = _normalize_suffix(code)
    variants = _suffix_variants(map_)
    if key in variants:
        return variants[key]

    # Fallback: match against any alias code suffix across stations.
    for station, info in _stations(map_).items():
        for group in ("aliases", "legacy_aliases", "low_aliases", "low_legacy_aliases"):
            for tk in info.get(group, []):
                if _normalize_suffix(_code_suffix(tk)) == key:
                    return station
    return None


def resolve_ticker(ticker: str, map_: Optional[dict] = None) -> Optional[str]:
    """Resolve any KXHIGH*/KXLOW* ticker to a canonical station ICAO (or None).

    Handles current + legacy + duplicate tickers across all five naming
    conventions. A rename/add resolves to the SAME station, never a new one.
    """
    if map_ is None:
        map_ = load_map()
    t = (ticker or "").strip().upper()
    if not (t.startswith("KXHIGH") or t.startswith("KXLOW")):
        return None
    # Direct alias match first (fastest, exact).
    for station, info in _stations(map_).items():
        for group in ("aliases", "legacy_aliases", "low_aliases", "low_legacy_aliases"):
            if t in info.get(group, []):
                return station
    # Fallback: normalize the code suffix.
    return station_for_code_suffix(_code_suffix(t), map_)


def aliases_for(station: str, map_: Optional[dict] = None) -> Dict[str, List[str]]:
    """Return {'HIGH': [...current+legacy...], 'LOW': [...current+legacy...]}."""
    if map_ is None:
        map_ = load_map()
    info = _stations(map_).get(station, {})
    high = list(info.get("aliases", [])) + list(info.get("legacy_aliases", []))
    low = list(info.get("low_aliases", [])) + list(info.get("low_legacy_aliases", []))
    return {"HIGH": high, "LOW": low}


def live_high_aliases(station: str, map_: Optional[dict] = None) -> List[str]:
    if map_ is None:
        map_ = load_map()
    info = _stations(map_).get(station, {})
    return list(info.get("aliases", []))


def live_low_aliases(station: str, map_: Optional[dict] = None) -> List[str]:
    if map_ is None:
        map_ = load_map()
    info = _stations(map_).get(station, {})
    return list(info.get("low_aliases", []))


def canonical_stations(map_: Optional[dict] = None) -> List[str]:
    if map_ is None:
        map_ = load_map()
    return sorted(_stations(map_).keys())


def is_live_alias(ticker: str, map_: Optional[dict] = None) -> bool:
    """True if ticker is a CURRENT (live) alias, not a legacy/dead one."""
    if map_ is None:
        map_ = load_map()
    t = (ticker or "").strip().upper()
    for info in _stations(map_).values():
        if t in info.get("aliases", []) or t in info.get("low_aliases", []):
            return True
    return False


if __name__ == "__main__":
    # Smoke test / inspection.
    m = load_map()
    print(f"Canonical stations: {len(canonical_stations(m))}")
    for st in canonical_stations(m):
        a = aliases_for(st, m)
        print(f"  {st}: HIGH={a['HIGH']} LOW={a['LOW']}")
    print("\nSample resolutions:")
    for tk in ["KXHIGHNY", "KXHIGHNY0", "KXHIGHTATL", "KXHIGHTHOU", "KXHIGHHOU",
               "KXHIGHTSATX", "KXHIGHTSDF", "KXLOWTNYC", "KXLOWNY", "KXHIGHTLV",
               "KXHIGHCHI", "KXHIGHTMIN", "KXHIGHTNOLA", "KXLOWTCHI"]:
        print(f"  {tk:16s} -> {resolve_ticker(tk, m)}")
