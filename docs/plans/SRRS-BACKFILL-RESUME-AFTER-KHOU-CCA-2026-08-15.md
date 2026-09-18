# SRRS full-backfill resume after KHOU CCA — 2026-08-15 UTC

## Gate and immutable inputs

This plan resumes the approved SRRS CLI acquisition only. It consumes the verified v3 280-key KHOU disposition as immutable input; it does not consult v1/v2 statuses for the disposition decision. The preserved v2-full root remains incident evidence and is never overwritten.

- v3 root: `data/raw/srrs_cli_backfill/2026-08-15-khou-conflict-v3/`
- v3 deterministic rerun: `data/raw/srrs_cli_backfill/2026-08-15-khou-conflict-v3-rerun/`
- v3 `manifest.jsonl` SHA-256: `16252404f9ca10873269814bc5ad78d94347f2b3634a10626575b428b1b39b17`
- v3 `run_summary.json` SHA-256: `a3cea7a61ff3d8fd50dd457b1c90df164d3f37127f6289c3f5adf68508f6f014`
- v3 `review.json` SHA-256: `318f37e1ebeaa22a9d3e33ea633639f9378c3d417b5fe3a50a3a0cbfacc5cd23`
- v3 `checkpoint.json` SHA-256: `2607da85fe73587dc44835ae236842528766aae59ada6d933414b26982a10c9f`
- v2-full immutable manifest SHA-256: `2aebfdfebbe6c68c1f75669f7d4beca36030611cbc6c6567666e45a73fa14628`
- v2-full immutable run-summary SHA-256 (stale warning below): `b593067c4e0628cbf5632cca5bdf7454c6e4e82feb31c4de2d7616f3aed85b31`
- conflict-review policy SHA-256: `879d2b2f32383dc0edb21514d532b758f1105204756d7fc03cbb1edda3454faf`

### Exact KHOU CCA selection

For `KHOU|2025-01-26`, v3 retains two intermediate reports and two full-day reports plus 22 failure rows. The selected full-day report is `CDUS44 KHGX 271523 CCA`, **70°F / 55°F**, raw SHA-256 `3fe90b212b37027b8d868dfbc9176f09ffedcbb51d6f7368b58910dc31af2662`. It explicitly supersedes the uncorrected `CDUS44 KHGX 270730` **77°F / 55°F**, raw SHA-256 `beb5877637759c462455705ad9507e737242e29167b4b6982de27fd56db7ddf5`. This is the evidence-backed CCA rule, not a general latest-report rule.

The v3 disposition is 280/280 `accepted_full_day`, with 1 conflict resolved and 0 blocked conflicts. The v3 selected value is provenance/QC only; it does not authorize source acceptance, calibration, evaluator, production, trading, or sweeps.

### Stale v2 summary warning

`data/raw/srrs_cli_backfill/2026-08-15-v2-full/run_summary.json` is stale and must not be used as the current summary: it predates the final 280-key state and the KHOU disposition. Its hash is retained above for immutability accounting. The resume process uses the v3 row for KHOU and the preserved v2 raw/scan/attempt evidence for the already materialized 280-key prefix; final counts are regenerated from the final checkpoint/artifacts, never from this stale summary.

## Scope and command

Frozen scope is the 20 mapped stations and target dates 2025-01-15 through 2026-02-28 inclusive: 410 × 20 = **8,200 station/date keys**. Existing terminal keys through 2025-01-28 are copied into a fresh output root; acquisition resumes with remaining keys after 2025-01-28 and continues through 2026-02-28. All exact official SRRS `.scan` and HTTP range evidence, failures, attempts, duplicates, and checkpoints are retained. No proxy substitution or whole-ANX fetch is allowed.

Pre-resume validation:

```sh
pytest -q tests/test_srrs_cli_lineage.py tests/test_srrs_khou_conflict_review.py
sha256sum data/raw/srrs_cli_backfill/2026-08-15-khou-conflict-v3/{manifest.jsonl,run_summary.json,review.json,checkpoint.json} data/raw/srrs_cli_backfill/2026-08-15-khou-conflict-v3-rerun/{manifest.jsonl,run_summary.json,review.json,checkpoint.json}
```

Resume command (serialized official acquisition, default 0.30 s delay, five retries):

```sh
python3 scripts/resume_srrs_after_khou_cca.py \
  --v3 data/raw/srrs_cli_backfill/2026-08-15-khou-conflict-v3 \
  --v2-full data/raw/srrs_cli_backfill/2026-08-15-v2-full \
  --output data/raw/srrs_cli_backfill/2026-08-15-srrs-cli-backfill-final \
  --start 2025-01-15 --resume-after 2025-01-28 --end 2026-02-28 \
  --delay 0.30 --timeout 60 --retries 5
```

The output root is fresh and separate. The command first verifies v3 hashes and v2 immutability, then materializes the 280-key prefix and invokes the existing v2 lineage/range-integrity acquisition for the remaining keys. It atomically checkpoints after each terminal key. On any source/range/integrity/rate-limit failure it stops without synthesizing a terminal status; raw attempts and evidence remain.

## Storage and time estimate

Planning estimate is approximately 30,000–40,000 report/revision bodies and 150–250 MB raw for 8,200 keys, plus scan/listing/attempt metadata. At one globally serialized request every 0.30 s, expected wall time is several hours to roughly one day depending on listing breadth, retries, and archive latency. These are estimates only; actual runtime, request count, rate-limit events, raw bytes, and duplicate groups are measured from retained artifacts.

## Stop conditions

Stop immediately and preserve the checkpoint on: non-206 range response; invalid/mismatched `Content-Range`; body-length or SHA-256 mismatch; immutable-byte collision; missing/corrupt scan metadata; HTTP/archive access failure after policy retries; sustained 429/rate-limit exhaustion; unresolved lineage or non-CCA conflicting full-day revisions; duplicate/terminal-key accounting loss; any attempted v1 status reuse; any proxy/non-SRRS substitution; or any request to alter calibration, evaluator, production, trading, or sweep behavior. Missing/no-CLI and blocked rows remain explicit and are never imputed.

After acquisition, run the independent QC specification read-only, regenerate the final summary from the final checkpoint/artifacts, perform two offline deterministic canonical reruns and hash comparisons, and write the final continuity receipt. Do not claim source acceptance or enable calibration automatically.
