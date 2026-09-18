# GEFS Temperature Provenance / Geometry Rebuild
Date: 2026-08-15 UTC
Status: rebuild planning and QC active; no scoring

## Why rebuild
Existing GEFS archive rows cannot prove dated issue/availability, member identity, raw object provenance, station timezone/DST, or local-day HIGH/LOW valid geometry. Raw GRIB/NetCDF objects are gone and sidecars are orphaned. Old rows are not eligible for model comparison.

## Rebuild sequence
1. Expert source/implementation spec.
2. Independent QC contract.
3. Five-station/date pilot with raw GRIB/idx/object preservation and hashes.
4. Validate station/grid mapping, init/availability cutoff, member IDs, valid-time trajectory, local-day HIGH/LOW extrema, deterministic replay.
5. Only if pilot passes: full historical rebuild.
6. Run temperature geometry/model tournament on rebuilt data.

## Required per-object provenance
- source URL/request and response metadata;
- retrieval timestamp;
- model/init/run timestamp;
- availability timestamp;
- valid times and lead steps;
- station/grid mapping;
- member IDs and values;
- raw object and sidecar hashes;
- timezone/DST mapping version;
- code/config version.

## Geometry
For station-local date D:
- HIGH = max forecast trajectory values covering D;
- LOW = min forecast trajectory values covering D;
- valid timestamps and local-day inclusion must be explicit;
- no arbitrary step-24 scalar scoring.

## Boundaries
Old archive is retained for forensic reference only. No HMM, ECMWF combination, production, trading, or scoring until rebuilt lineage passes QC.
