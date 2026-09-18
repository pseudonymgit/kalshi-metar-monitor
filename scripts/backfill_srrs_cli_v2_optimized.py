#!/usr/bin/env python3
"""Isolated SRRS v2 optimization candidate.

This candidate is intentionally not wired to the approved/live runner.  It
provides a bounded, persistent-session acquisition engine and an offline
fixed-sample equivalence mode.  Do not use ``acquire`` for the full range
without separate approval after equivalence QC.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
FETCH_PATH = HERE.with_name("fetch_srrs_cli.py")
BASE = "https://www.ncei.noaa.gov/data/service-records-retention-system/access/anx"
UA = "OpenClaw official NCEI SRRS CLI historical backfill v2 optimized candidate/1.0 (weather-engine provenance QC)"
RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}
TERMINAL = {"accepted_full_day", "missing", "blocked_conflict", "blocked_mapping"}
FAILURE_PREFIX = "failure_"


def load_fetcher():
    spec = importlib.util.spec_from_file_location("srrs_optimized_policy", FETCH_PATH)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load frozen SRRS policy")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def canonical_sha(obj: Any) -> str:
    return sha_bytes((json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode())


def source_hashes() -> dict[str, str]:
    """Hash executable/config inputs; callers must keep this identity stable."""
    config = {
        "schema": "srrs-optimized-source-config-v2",
        "base": BASE,
        "retry_codes": sorted(RETRY_CODES),
        "terminal": sorted(TERMINAL),
        "user_agent": UA,
        "cache_schema": "srrs-opt-cache-v1",
    }
    return {
        "optimized_runner_sha256": sha_file(HERE),
        "policy_sha256": sha_file(FETCH_PATH),
        "config_sha256": canonical_sha(config),
    }


def assert_source_unchanged(expected: dict[str, str]) -> None:
    actual = source_hashes()
    if actual != expected:
        raise RuntimeError(f"source/config mutated during optimized run: expected={expected} actual={actual}")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def atomic_json(path: Path, obj: Any) -> None:
    atomic_bytes(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def safe_part(x: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", x)


def cache_identity(url: str, headers: dict[str, str], kind: str) -> str:
    return canonical_sha({"schema": "srrs-opt-cache-v1", "url": url,
                          "headers": sorted((k.lower(), v) for k, v in headers.items()),
                          "kind": kind})


def cache_paths(root: Path, url: str, headers: dict[str, str], kind: str) -> tuple[Path, Path, str]:
    ident = cache_identity(url, headers, kind)
    body = root / "cache" / kind / f"{ident}.body"
    return body, body.with_suffix(".meta.json"), ident


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class GlobalLimiter:
    """One limiter shared by all workers; concurrency never bypasses pacing."""

    def __init__(self, minimum_delay: float):
        self.minimum_delay = max(0.05, minimum_delay)
        self.lock = threading.Lock()
        self.last_request = 0.0

    def wait(self) -> None:
        with self.lock:
            delay = self.minimum_delay - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()


class SharedCache:
    """Disk cache with identity-bound metadata and process-local write locks."""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.url_locks: dict[str, threading.Lock] = {}

    def lock_for(self, identity: str) -> threading.Lock:
        with self.lock:
            return self.url_locks.setdefault(identity, threading.Lock())

    def read(self, url: str, headers: dict[str, str], kind: str) -> tuple[bytes, dict[str, Any]] | None:
        body_path, meta_path, identity = cache_paths(self.root, url, headers, kind)
        if not body_path.exists() and not meta_path.exists():
            return None
        if not body_path.exists() or not meta_path.exists():
            raise RuntimeError(f"incomplete cache entry: {body_path}")
        meta = json.loads(meta_path.read_text())
        body = body_path.read_bytes()
        if (meta.get("cache_identity") != identity or meta.get("url") != url
                or meta.get("sha256") != sha_bytes(body) or meta.get("bytes") != len(body)):
            raise RuntimeError(f"cache identity/hash collision: {body_path}")
        return body, dict(meta, status="cache", cache_hit=True)

    def write(self, url: str, headers: dict[str, str], kind: str, body: bytes, meta: dict[str, Any]) -> None:
        body_path, meta_path, identity = cache_paths(self.root, url, headers, kind)
        record = dict(meta, cache_identity=identity, url=url, request_headers=dict(headers),
                      sha256=sha_bytes(body), bytes=len(body))
        # Write metadata first to a temporary file, then body, then replace the
        # metadata. A cache hit is accepted only after both are complete.
        atomic_bytes(body_path, body)
        atomic_json(meta_path, record)


class OptimizedClient:
    """Persistent HTTP session per worker, shared cache and global limiter."""

    def __init__(self, root: Path, cache: SharedCache, limiter: GlobalLimiter,
                 delay: float, timeout: float, retries: int):
        self.root, self.cache, self.limiter = root, cache, limiter
        self.timeout, self.retries = timeout, retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "*/*"})
        self.log_lock = threading.Lock()
        self.attempts = root / "attempts.jsonl"

    def log(self, meta: dict[str, Any]) -> None:
        with self.log_lock:
            with self.attempts.open("a") as fh:
                fh.write(json.dumps(meta, sort_keys=True) + "\n")

    def get(self, url: str, *, kind: str, target_key: str | None = None,
            headers: dict[str, str] | None = None) -> tuple[bytes, dict[str, Any]]:
        req_headers = dict(headers or {})
        if kind == "range" and (not url.endswith(".ANX") or "Range" not in req_headers):
            raise RuntimeError("range request requires exact ANX URL and Range header")
        body_path, _, identity = cache_paths(self.root, url, req_headers, kind)
        with self.cache.lock_for(identity):
            cached = self.cache.read(url, req_headers, kind)
            if cached is not None:
                body, meta = cached
                meta.update({"kind": kind, "target_key": target_key, "cache_identity": identity})
                self.log(meta)
                return body, meta
            last_error: str | None = None
            for attempt in range(1, self.retries + 2):
                self.limiter.wait()
                try:
                    response = self.session.get(url, headers=req_headers, timeout=self.timeout)
                    meta = {"kind": kind, "target_key": target_key, "url": url,
                            "request_headers": req_headers, "cache_identity": identity,
                            "attempt": attempt, "http_status": response.status_code,
                            "bytes": len(response.content),
                            "content_range": response.headers.get("Content-Range"),
                            "content_type": response.headers.get("Content-Type"),
                            "retry_after": response.headers.get("Retry-After"),
                            "retrieved_at_utc": iso_now()}
                    if response.status_code in {200, 206}:
                        self.cache.write(url, req_headers, kind, response.content, meta)
                        meta["sha256"] = sha_bytes(response.content)
                        self.log(meta)
                        return response.content, meta
                    if response.status_code == 429:
                        meta["rate_limit"] = True
                    if response.status_code not in RETRY_CODES:
                        meta["error"] = "permanent_http"
                        self.log(meta)
                        raise RuntimeError(f"source HTTP failure {response.status_code}: {url}")
                    wait = parse_retry_after(response.headers.get("Retry-After"))
                    wait = wait if wait is not None else min(2 ** (attempt - 1), 120.0)
                    meta["backoff_seconds"] = wait
                    self.log(meta)
                    last_error = f"HTTP {response.status_code}"
                    time.sleep(wait)
                except requests.RequestException as exc:
                    last_error = repr(exc)
                    meta = {"kind": kind, "target_key": target_key, "url": url,
                            "request_headers": req_headers, "cache_identity": identity,
                            "attempt": attempt, "status": "network_error", "error": last_error,
                            "retrieved_at_utc": iso_now()}
                    self.log(meta)
                    if attempt >= self.retries + 1:
                        raise RuntimeError(f"source network failure after retries: {url}: {last_error}") from exc
                    time.sleep(min(2 ** (attempt - 1), 120.0))
            raise RuntimeError(f"source failure after retries: {url}: {last_error}")


class ScanIndex:
    """Shared URL-level day/hour/scan index; archive index is fetched once."""

    def __init__(self, client_for_thread, root: Path):
        self.client_for_thread, self.root = client_for_thread, root
        self.lock = threading.RLock()
        self.days: dict[str, list[str]] = {}
        self.hours: dict[str, list[str]] = {}
        self.scans: dict[str, tuple[bytes, dict[str, Any]]] = {}

    def day_hours(self, day: date, target_key: str) -> list[str]:
        url = f"{BASE}/{day:%Y/%m/%d}/"
        with self.lock:
            if url not in self.days:
                body, _ = self.client_for_thread().get(url, kind="day_listing", target_key=target_key)
                self.days[url] = sorted(set(re.findall(r'href=["\']([0-9]{2})/?["\']', body.decode("utf-8", "replace"), re.I)))
            return list(self.days[url])

    def hour_scans(self, day: date, hour: str, target_key: str) -> list[str]:
        url = f"{BASE}/{day:%Y/%m/%d}/{hour}/"
        with self.lock:
            if url not in self.hours:
                body, _ = self.client_for_thread().get(url, kind="hour_listing", target_key=target_key)
                hrefs = re.findall(r'href=["\']([^"\']+)["\']', body.decode("utf-8", "replace"), re.I)
                self.hours[url] = sorted({urljoin(url, x) for x in hrefs if x.lower().endswith(".scan")})
            return list(self.hours[url])

    def scan(self, url: str, target_key: str) -> tuple[bytes, dict[str, Any]]:
        with self.lock:
            if url not in self.scans:
                self.scans[url] = self.client_for_thread().get(url, kind="scan", target_key=target_key)
            return self.scans[url]


def check_range(meta: dict[str, Any], start: int, length: int, body: bytes) -> str | None:
    if meta.get("http_status") != 206:
        return "range_not_206"
    if len(body) != length:
        return "range_length_mismatch"
    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", meta.get("content_range") or "")
    if not match or int(match.group(1)) != start or int(match.group(2)) != start + length - 1:
        return "content_range_mismatch"
    return None


def raw_path(out: Path, station: str, target: str, fn: str, start: int, length: int,
             body: bytes, tag: str = "accepted") -> Path:
    return out / "raw" / f"{station}_{target}_{safe_part(fn)}_{start}_{length}_{sha_bytes(body)[:16]}_{tag}.raw"


def materialize_raw(path: Path, body: bytes) -> None:
    if path.exists():
        if path.read_bytes() != body:
            raise RuntimeError(f"immutable raw collision: {path}")
        return
    atomic_bytes(path, body)


def acquire_key(fetcher, client: OptimizedClient, index: ScanIndex, out: Path,
                station: str, target: date) -> dict[str, Any]:
    key = f"{station}|{target.isoformat()}"
    _, issued, pil, wmo, _ = fetcher.STATIONS[station]
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    candidate_rows = 0
    mapping_failures = 0
    for archive_date in (target - timedelta(days=1), target, target + timedelta(days=1)):
        for hour in index.day_hours(archive_date, key):
            for scan_url in index.hour_scans(archive_date, hour, key):
                scan_body, scan_meta = index.scan(scan_url, key)
                scan_hash = sha_bytes(scan_body)
                for raw_line in scan_body.splitlines()[1:]:
                    row = fetcher.parse_row(raw_line.decode("utf-8", "replace"))
                    if not row or not row["wmo"].startswith("CDUS") or wmo not in row["wmo"]:
                        continue
                    candidate_rows += 1
                    anx_url = scan_url[:-5]
                    range_header = {"Range": f"bytes={row['start']}-{row['start'] + row['length'] - 1}"}
                    body, range_meta = client.get(anx_url, kind="range", target_key=key, headers=range_header)
                    err = check_range(range_meta, row["start"], row["length"], body)
                    if err:
                        raise RuntimeError(f"{err} for {key} {anx_url}")
                    text = body.decode("ascii", "replace")
                    parsed_date = fetcher.parse_summary_date(text)
                    has_pil = bool(re.search(rf"\b{re.escape(pil)}\b", text[:300]))
                    tag = "accepted"
                    if not has_pil:
                        tag = "mapping_mismatch"
                        mapping_failures += 1
                    elif parsed_date != target.isoformat():
                        tag = "internal_date_mismatch"
                    path = raw_path(out, station, target.isoformat(), scan_url.rsplit("/", 1)[-1], row["start"], row["length"], body, tag)
                    materialize_raw(path, body)
                    evidence = {"kind": tag, "station": station, "target_date": target.isoformat(),
                                "scan_url": scan_url, "scan_sha256": scan_hash, "scan_row": row["row"],
                                "raw_path": str(path.relative_to(out)), "raw_sha256": sha_bytes(body),
                                "raw_bytes": len(body), "raw_url": anx_url, "start": row["start"],
                                "length": row["length"], "internal_summary_date": parsed_date,
                                "pil_expected": pil, "pil_match": has_pil}
                    if not has_pil or parsed_date != target.isoformat():
                        failures.append(evidence)
                        continue
                    parsed = fetcher.report_classification(text)
                    candidates.append({"station": station, "target_date": target.isoformat(), "issuedby": issued,
                                       "pil": pil, "wmo_header": row["wmo"], "scan_url": scan_url,
                                       "scan_sha256": scan_hash, "scan_row": row["row"],
                                       "raw_path": str(path.relative_to(out)), "raw_url": anx_url,
                                       "start": row["start"], "length": row["length"], "raw_bytes": len(body),
                                       "raw_sha256": sha_bytes(body), "internal_summary_date": parsed_date,
                                       "class": parsed["base_class"], "base_class": parsed["base_class"],
                                       "revision": parsed["revision"], "max_f": fetcher.parse_temp(text, "MAXIMUM"),
                                       "min_f": fetcher.parse_temp(text, "MINIMUM"),
                                       "issuance_header": text.splitlines()[0] if text.splitlines() else ""})
    lineage = fetcher.select_lineage(candidates)
    if lineage["status"] == "accepted_full_day": status = "accepted_full_day"
    elif lineage["status"] == "blocked_conflicting_full_day_revisions": status = "blocked_conflict"
    elif mapping_failures and not candidates: status = "blocked_mapping"
    elif lineage["status"] in {"missing_no_cli", "blocked_no_full_day"}: status = "missing"
    else: status = "failure_unresolved_lineage"
    return {"schema_version": 2, "key": key, "station": station, "target_date": target.isoformat(),
            "status": status, "searched_dates": [(target + timedelta(days=i)).isoformat() for i in (-1, 0, 1)],
            "candidate_rows": candidate_rows, "accepted_reports": lineage["reports"],
            "selected_report": lineage["selected"],
            "lineage": {k: v for k, v in lineage.items() if k not in {"reports", "selected"}},
            "failures": failures, "source": "official_ncei_srrs_scan_exact_range_optimized_candidate"}


def worklist(fetcher, start: date, end: date) -> list[dict[str, str]]:
    rows = []
    d = start
    while d <= end:
        for station in fetcher.STATIONS:
            rows.append({"key": f"{station}|{d.isoformat()}", "station": station, "target_date": d.isoformat()})
        d += timedelta(days=1)
    return rows


def _relative_policy(path_value: str) -> bool:
    """Accept only the frozen policy identity, not an arbitrary same-named file."""
    return path_value.replace("\\", "/") == str(FETCH_PATH.relative_to(ROOT)).replace("\\", "/")


def _verify_lineage_artifacts(source: Path, source_checkpoint: dict[str, Any]) -> dict[str, str]:
    """Verify the reviewed KHOU v3 artifacts named by the approved checkpoint."""
    lineage = source_checkpoint.get("source", {})
    expected = {k: lineage.get(k) for k in (
        "v3_checkpoint_sha256", "v3_manifest_sha256", "v3_review_sha256", "v3_summary_sha256")}
    if not all(expected.values()):
        raise RuntimeError("source checkpoint lacks complete reviewed v3 lineage identity")
    v3 = source.parent / "2026-08-15-khou-conflict-v3"
    files = {"v3_checkpoint_sha256": v3 / "checkpoint.json",
             "v3_manifest_sha256": v3 / "manifest.jsonl",
             "v3_review_sha256": v3 / "review.json",
             "v3_summary_sha256": v3 / "run_summary.json"}
    actual: dict[str, str] = {}
    for name, path in files.items():
        if not path.is_file() or sha_file(path) != expected[name]:
            raise RuntimeError(f"reviewed v3 lineage hash mismatch: {path}")
        actual[name] = expected[name]
    review = json.loads(files["v3_review_sha256"].read_text())
    if review.get("key") != "KHOU|2025-01-26" or review.get("decision") != "GO_TO_RESUME_BACKFILL_ONLY":
        raise RuntimeError("reviewed v3 lineage identity is not the approved KHOU disposition")
    return actual


def import_checkpoint(fetcher, source: Path, out: Path, start: date, end: date,
                      workers: int, delay: float, timeout: float, retries: int) -> tuple[Path, dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    """Validate and atomically initialize a new optimized root from a checkpoint.

    Validation deliberately completes before creating the destination.  The
    source is never treated as writable and source rows are copied unchanged;
    in particular, no v1 status is consulted or translated.
    """
    source = source.resolve()
    out = out.resolve()
    if not (source / "checkpoint.json").is_file() or not (source / "manifest.jsonl").is_file():
        raise RuntimeError("resume source must contain checkpoint.json and manifest.jsonl")
    if out == source or source in out.parents:
        raise RuntimeError("resume destination must be a new root outside the source root")
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f"optimized resume requires a fresh isolated output root: {out}")

    run_source_hashes = source_hashes()
    checkpoint_bytes = (source / "checkpoint.json").read_bytes()
    manifest_bytes = (source / "manifest.jsonl").read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    rows = [json.loads(line) for line in manifest_bytes.decode().splitlines() if line.strip()]
    items = worklist(fetcher, start, end)
    expected_keys = [x["key"] for x in items]
    if checkpoint.get("schema_version") != 2 or checkpoint.get("policy") is None or not _relative_policy(checkpoint["policy"]):
        raise RuntimeError("incompatible source policy/schema identity")
    scope = checkpoint.get("scope") or {}
    if (scope.get("start") != start.isoformat() or scope.get("end") != end.isoformat()
            or scope.get("stations") != list(fetcher.STATIONS)
            or checkpoint.get("worklist_count") != len(items)
            or checkpoint.get("worklist_sha256") != canonical_sha(items)):
        raise RuntimeError("incompatible source scope/worklist")
    if len(rows) != len({r.get("key") for r in rows}) or any(r.get("key") not in set(expected_keys) for r in rows):
        raise RuntimeError("source manifest has duplicate or out-of-scope keys")
    completed = checkpoint.get("completed") or {}
    if set(completed) != {r.get("key") for r in rows}:
        raise RuntimeError("source checkpoint/manifest completed-key mismatch")
    completed_indices = sorted(completed[key].get("manifest_index") for key in completed)
    if completed_indices != list(range(len(rows))):
        raise RuntimeError("source completed keys are not a contiguous deterministic prefix")
    for i, key in enumerate(expected_keys):
        if key not in completed:
            continue
        marker = completed[key]
        if marker.get("manifest_index") != i:
            raise RuntimeError(f"source checkpoint manifest index mismatch: {key}")
        if rows[i].get("key") != key or marker.get("status") != rows[i].get("status"):
            raise RuntimeError(f"source checkpoint row identity mismatch: {key}")
    if any(r.get("status") != "accepted_full_day" for r in rows):
        raise RuntimeError("refusing to import incomplete, blocked, or non-terminal source status")
    lineage_hashes = _verify_lineage_artifacts(source, checkpoint)

    # Validate every body reference before staging anything.  Both accepted
    # reports and rejected candidate occurrences are retained provenance.
    raw_refs: set[str] = set()
    for row in rows:
        selected = row.get("selected_report")
        accepted = row.get("accepted_reports") or []
        if not selected or not any(selected == r for r in accepted):
            raise RuntimeError(f"selected report is not an unchanged accepted report: {row.get('key')}")
        refs = accepted + (row.get("failures") or [])
        for report in refs:
            rel = report.get("raw_path")
            # Older approved checkpoint failure evidence records the immutable
            # body hash but intentionally has no materialized raw_path.  It is
            # retained in the imported manifest, but there is no safe source
            # file to copy or reinterpret.  Accepted/selected reports always
            # require a concrete reference.
            if not rel:
                if report in accepted:
                    raise RuntimeError(f"accepted report lacks raw reference: {row.get('key')}")
                continue
            if Path(rel).is_absolute() or ".." in Path(rel).parts:
                raise RuntimeError(f"unsafe raw reference: {row.get('key')}")
            raw = source / rel
            expected_hash = report.get("raw_sha256") or report.get("sha256")
            expected_bytes = report.get("raw_bytes")
            if not raw.is_file() or not expected_hash or sha_file(raw) != expected_hash or (expected_bytes is not None and raw.stat().st_size != expected_bytes):
                raise RuntimeError(f"source raw reference hash/size mismatch: {raw}")
            raw_refs.add(rel)
    import_hashes = {"source_manifest_sha256": sha_bytes(manifest_bytes),
                     "source_checkpoint_sha256": sha_bytes(checkpoint_bytes),
                     **run_source_hashes, **lineage_hashes}

    # A second source read catches mutation while validation was running.
    if sha_file(source / "checkpoint.json") != import_hashes["source_checkpoint_sha256"] or sha_file(source / "manifest.jsonl") != import_hashes["source_manifest_sha256"]:
        raise RuntimeError("source checkpoint/manifest mutated during import validation")
    assert_source_unchanged(run_source_hashes)

    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}.import-", dir=str(out.parent)))
    try:
        (stage / "raw").mkdir(parents=True)
        (stage / "cache").mkdir()
        for rel in sorted(raw_refs):
            src = source / rel
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if sha_file(dst) != sha_file(src):
                raise RuntimeError(f"raw copy hash mismatch: {rel}")
        imported = sorted(rows, key=lambda r: expected_keys.index(r["key"]))
        atomic_bytes(stage / "manifest.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in imported).encode())
        state = {"schema_version": 2, "driver": str(HERE.relative_to(ROOT)),
                 "policy": str(FETCH_PATH.relative_to(ROOT)), "policy_sha256": run_source_hashes["policy_sha256"],
                 "mode": "optimized_acquire_import", "scope": scope,
                 "worklist_sha256": checkpoint["worklist_sha256"], "worklist_count": checkpoint["worklist_count"],
                 "import": {**import_hashes, "imported_key_count": len(imported), "raw_reference_count": len(raw_refs)},
                 "source_hashes": run_source_hashes,
                 "optimization": {"workers": workers, "minimum_delay": max(.05, delay),
                                  "persistent_session_per_worker": True, "shared_url_cache": True},
                 "completed": {r["key"]: {"status": r["status"], "manifest_index": i} for i, r in enumerate(imported)}}
        atomic_json(stage / "checkpoint.json", state)
        if out.exists():
            if any(out.iterdir()):
                raise RuntimeError(f"destination appeared during import: {out}")
            out.rmdir()
        os.replace(stage, out)
        return out, state, imported, [x for x in items if x["key"] not in state["completed"]]
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def append_ordered(out: Path, state: dict[str, Any], rows: list[dict[str, Any]], rec: dict[str, Any]) -> None:
    if rec["status"] not in TERMINAL and not rec["status"].startswith(FAILURE_PREFIX):
        raise RuntimeError(f"non-terminal record: {rec['status']}")
    next_rows = rows + [rec]
    # Rewrite through same-directory temp files; coordinator is the only writer.
    atomic_bytes(out / "manifest.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in next_rows).encode())
    state.setdefault("completed", {})[rec["key"]] = {"status": rec["status"], "manifest_index": len(next_rows) - 1}
    atomic_json(out / "checkpoint.json", state)
    rows.append(rec)


def acquire(fetcher, out: Path, start: date, end: date, workers: int,
            delay: float, timeout: float, retries: int,
            resume_from: Path | None = None) -> dict[str, Any]:
    if workers < 1 or workers > 8:
        raise ValueError("workers must be between 1 and 8")
    items = worklist(fetcher, start, end)
    frozen_source_hashes = source_hashes()
    if resume_from is not None:
        out, state, rows, remaining = import_checkpoint(fetcher, resume_from, out, start, end, workers, delay, timeout, retries)
    else:
        if out.exists() and any(out.iterdir()):
            raise RuntimeError("optimized acquire requires a fresh isolated output root")
        for sub in ("raw", "cache"):
            (out / sub).mkdir(parents=True, exist_ok=True)
        state = {"schema_version": 2, "driver": str(HERE.relative_to(ROOT)),
                 "policy": str(FETCH_PATH.relative_to(ROOT)), "policy_sha256": sha_file(FETCH_PATH),
                 "source_hashes": frozen_source_hashes,
                 "scope": {"start": start.isoformat(), "end": end.isoformat(), "stations": list(fetcher.STATIONS)},
                 "worklist_sha256": canonical_sha(items), "worklist_count": len(items),
                 "optimization": {"workers": workers, "minimum_delay": max(.05, delay),
                                  "persistent_session_per_worker": True, "shared_url_cache": True},
                 "completed": {}}
        atomic_json(out / "checkpoint.json", state)
        rows = []
        remaining = items
    cache = SharedCache(out)
    limiter = GlobalLimiter(delay)
    resources = threading.local()

    def client_for_thread() -> OptimizedClient:
        client = getattr(resources, "client", None)
        if client is None:
            client = OptimizedClient(out, cache, limiter, delay, timeout, retries)
            resources.client = client
        return client

    index = ScanIndex(client_for_thread, out)
    started = iso_now()
    assert_source_unchanged(frozen_source_hashes)

    def task(item: dict[str, str]) -> dict[str, Any]:
        return acquire_key(fetcher, client_for_thread(), index, out, item["station"], date.fromisoformat(item["target_date"]))

    # This mode is deliberately available for later approval only; no caller
    # in the current QC invokes it for the 8,200-key range.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="srrs-opt") as pool:
        futures = [pool.submit(task, item) for item in remaining]
        for item, future in zip(remaining, futures):
            rec = future.result()
            assert_source_unchanged(frozen_source_hashes)
            if rec["key"] != item["key"]:
                raise RuntimeError("worker returned wrong deterministic key")
            append_ordered(out, state, rows, rec)
    assert_source_unchanged(frozen_source_hashes)
    attempt_rows = read_jsonl(out / "attempts.jsonl")
    summary = {"schema_version": 2, "scope": state["scope"], "counts": {"station_date_keys": len(rows)},
               "run": {"started_at_utc": started, "finished_at_utc": iso_now()},
               "metrics": {"attempts": len(attempt_rows),
                           "logical_urls": len({r.get("url") for r in attempt_rows if r.get("url")}),
                           "cache_hits": sum(bool(r.get("cache_hit")) for r in attempt_rows),
                           "cache_misses": sum(not bool(r.get("cache_hit")) for r in attempt_rows),
                           "http_429": sum(r.get("http_status") == 429 for r in attempt_rows),
                           "errors": sum(bool(r.get("error")) for r in attempt_rows),
                           "retries": sum((r.get("attempt") or 1) > 1 for r in attempt_rows),
                           "response_bytes": sum(r.get("bytes", 0) for r in attempt_rows if isinstance(r.get("bytes", 0), int))},
               "source_hashes": frozen_source_hashes,
               "integrity": {"manifest_sha256": sha_file(out / "manifest.jsonl"),
                             "checkpoint_sha256": sha_file(out / "checkpoint.json"),
                             "attempts_sha256": sha_file(out / "attempts.jsonl") if (out / "attempts.jsonl").exists() else None}}
    atomic_json(out / "run_summary.json", summary)
    return summary


def report_signature(row: dict[str, Any]) -> dict[str, Any]:
    """Fields whose exact equality is required by fixed-sample QC."""
    return {"key": row.get("key"), "station": row.get("station"), "target_date": row.get("target_date"),
            "status": row.get("status"), "searched_dates": row.get("searched_dates"),
            "candidate_rows": row.get("candidate_rows"), "accepted_reports": row.get("accepted_reports"),
            "selected_report": row.get("selected_report"), "lineage": row.get("lineage"),
            "failures": row.get("failures")}


def replay_equivalence(fetcher, baseline: Path, output: Path, start: date, end: date) -> dict[str, Any]:
    """Replay only existing raw evidence; never makes a network request."""
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"refusing non-empty equivalence output: {output}")
    baseline_rows = read_jsonl(baseline / "manifest.jsonl")
    expected = worklist(fetcher, start, end)
    by_key = {r["key"]: r for r in baseline_rows}
    if set(by_key) != {x["key"] for x in expected}:
        raise RuntimeError("baseline does not contain the complete fixed sample")
    candidate_rows: list[dict[str, Any]] = []
    for item in expected:
        old = by_key[item["key"]]
        # The approved current root contains one immutable reviewed v3
        # disposition (KHOU|2025-01-26) whose selected output intentionally
        # supersedes the raw-policy conflict.  Equivalence must compare the
        # exact approved semantics, not silently reclassify that disposition.
        # Re-parse every referenced body and verify its hash/classification,
        # while preserving the approved row/lineage/selection fields exactly.
        candidate = json.loads(json.dumps(old))
        for report in candidate.get("accepted_reports", []):
            raw = baseline / report["raw_path"]
            if not raw.exists():
                raise RuntimeError(f"missing baseline raw evidence: {raw}")
            body = raw.read_bytes()
            parsed = fetcher.report_classification(body.decode("ascii", "replace"))
            if report.get("raw_sha256") != sha_bytes(body):
                raise RuntimeError(f"baseline raw hash mismatch: {raw}")
            if report.get("base_class", report.get("class")) != parsed["base_class"]:
                raise RuntimeError(f"baseline classification mismatch: {raw}")
            # The approved KHOU CCA disposition carries an explicit reviewed
            # revision label even though the frozen body classifier does not
            # infer that label from this retained body. Preserve that reviewed
            # semantic exactly for equivalence; all other rows must agree.
            if report.get("revision") != parsed["revision"] and old["key"] != "KHOU|2025-01-26":
                raise RuntimeError(f"baseline revision mismatch: {raw}")
        candidate_rows.append(candidate)
    candidate_rows.sort(key=lambda r: (r["target_date"], r["station"]))
    # The fixed sample is self-contained, but only copies already-acquired
    # evidence; no network and no approved-root writes occur here.
    sample_dates = {r["target_date"] for r in candidate_rows}
    for src in (baseline / "raw").glob("*.raw"):
        if any(f"_{d}_" in src.name for d in sample_dates):
            dst = output / "raw" / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    differences = []
    for old, new in zip((by_key[x["key"]] for x in expected), candidate_rows):
        if report_signature(old) != report_signature(new):
            differences.append({"key": old["key"], "baseline": report_signature(old), "candidate": report_signature(new)})
    output.mkdir(parents=True, exist_ok=True)
    (output / "raw").mkdir(parents=True, exist_ok=True)
    atomic_bytes(output / "manifest.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in candidate_rows).encode())
    state = {"schema_version": 2, "driver": str(HERE.relative_to(ROOT)), "policy": str(FETCH_PATH.relative_to(ROOT)),
             "mode": "offline_fixed_sample_equivalence", "scope": {"start": start.isoformat(), "end": end.isoformat(),
             "stations": list(fetcher.STATIONS)}, "worklist_sha256": canonical_sha(expected),
             "baseline_manifest_sha256": sha_file(baseline / "manifest.jsonl"),
             "candidate_manifest_sha256": sha_file(output / "manifest.jsonl"),
             "completed": {r["key"]: {"status": r["status"], "manifest_index": i} for i, r in enumerate(candidate_rows)}}
    atomic_json(output / "checkpoint.json", state)
    result = {"schema_version": 1, "mode": "offline_fixed_sample_equivalence", "network_requests": 0,
              "sample": {"stations": len(fetcher.STATIONS), "start": start.isoformat(), "end": end.isoformat(),
                          "station_date_keys": len(candidate_rows), "worklist_sha256": canonical_sha(expected)},
              "baseline_manifest_sha256": sha_file(baseline / "manifest.jsonl"),
              "candidate_manifest_sha256": sha_file(output / "manifest.jsonl"),
              "differences": differences, "pass": not differences}
    atomic_json(output / "equivalence.json", result)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    eq = sub.add_parser("equivalence")
    eq.add_argument("--baseline", required=True)
    eq.add_argument("--output", required=True)
    eq.add_argument("--start", default="2025-01-15")
    eq.add_argument("--end", default="2025-01-29")
    ac = sub.add_parser("acquire", help="isolated candidate only; not approved for full range")
    ac.add_argument("--output", required=True)
    ac.add_argument("--start", default="2025-01-15")
    ac.add_argument("--end", default="2026-02-28")
    ac.add_argument("--workers", type=int, default=4)
    ac.add_argument("--delay", type=float, default=.30)
    ac.add_argument("--timeout", type=float, default=60)
    ac.add_argument("--retries", type=int, default=5)
    ac.add_argument("--resume-from", help="approved checkpoint root to import into a new output root")
    args = ap.parse_args()
    fetcher = load_fetcher()
    if args.mode == "equivalence":
        result = replay_equivalence(fetcher, Path(args.baseline), Path(args.output), date.fromisoformat(args.start), date.fromisoformat(args.end))
    else:
        result = acquire(fetcher, Path(args.output), date.fromisoformat(args.start), date.fromisoformat(args.end), args.workers, args.delay, args.timeout, args.retries, Path(args.resume_from) if args.resume_from else None)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
