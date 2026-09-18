# GHCN-Daily calibration-source acquisition specification (2026-08-15)

## Scope and disposition
Bounded, read-only NOAA/NCEI GHCN-Daily acquisition for the exact 20 weather-engine stations. Calibration remains disabled; no evaluator, production, or trading path is changed. Candidate data are preserved for audit only. **Disposition: BLOCKED/NEEDS_REVIEW** — official provenance does not establish equivalence to the NWS CLI/Kalshi settlement semantics, and no post-freeze descriptive settlement comparison was run.

## Pinned source and commands
Official endpoints only: `https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt`, `ghcnd-inventory.txt`, `readme.txt`, and `/all/{GHCN_ID}.dly`. Retrieved 2026-08-15 UTC; endpoint does not expose a separately named release, so the retrieval manifest and SHA-256 hashes pin this snapshot. Commands are recorded in `data/external/ghcn_daily/manifests/acquisition_manifest_20260815.json`.

## Mapping/QC
Mapping uses the authoritative `data/station_mapping.json` weather-engine universe and official `ghcnd-stations.txt` metadata. Exact airport/observatory matches: KATL USW00013874; KAUS USW00013904; KBOS USW00014739; KDCA USW00013743; KDEN USW00003017; KDFW USW00003927; KHOU USW00012918; KLAS USW00023169; KLAX USW00023174; KMDW USW00094846; KMIA USW00012839; KMSP USW00014922; KMSY USW00012916; KNYC USW00094728; KOKC USW00013967; KPHL USW00013739; KPHX USW00023183; KSAT USW00012921; KSEA USW00024233; KSFO USW00023234. No ambiguous mappings were accepted. Coordinates and rationale are in the derived audit.

## Semantics
GHCN `.dly` TMAX is tenths of degrees C (`value / 10`), with `-9999` missing. Element records are fixed-width daily values; each value carries measurement, quality, source, and time-of-observation flags. Raw files and all flags/revisions are untouched. Directional candidate uses only adjacent daily TMAX after explicit date/units parsing; no calibration fitting.

## Acceptance gates/results
Raw files include history through the requested cutoff and prior day where available; duplicates and flags are counted, never silently collapsed. Deterministic audit: `python3 scripts/audit_ghcn_daily_calibration_source.py`. Hashes, byte sizes, URLs, commands, and retrieval time are in the manifest. Kalshi settlements were not used to construct labels. A descriptive post-freeze comparison is pending, so no equivalence claim is made.

## Limitation and improvement
Primary limitation is station/semantic mismatch risk: airport GHCN TMAX and NWS CLI settlement may differ in observation-day and revision rules. Accuracy-focused improvement: obtain an immutable post-freeze NWS CLI/Kalshi settlement extract and compare date-aligned TMAX direction, flagging disagreement by station, then review only—not tune labels to agreement.
