#!/usr/bin/env python3
"""Bounded official NOAA GEFS provenance pilot.

This tool intentionally acquires only the frozen pilot fixtures described in
GEFS-TEMPERATURE-PILOT-ACQUISITION-SPEC-2026-08-15.md. It never opens the
legacy GEFS archive and never computes scores or trading signals.
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, sys, time, urllib.error, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
# The frozen contract is >=10.0 s between the previous response boundary and
# the next request boundary.  Keep a small scheduling margin, but prove the
# contract independently with time.monotonic_ns() at the actual urlopen edge.
MIN_REQUEST_GAP_SECONDS = 10.0
DELAY = 10.2
MEMBERS = ("gec00", "gep01")
LEADS = tuple(range(6, 55, 3))
STATIONS = {
    "KNYC": {"lat": 40.7772, "lon": -73.8726, "tz": "America/New_York"},
    "KDEN": {"lat": 39.8617, "lon": -104.6731, "tz": "America/Denver"},
    "KPHX": {"lat": 33.4342, "lon": -112.0116, "tz": "America/Phoenix"},
    "KSEA": {"lat": 47.4499, "lon": -122.3118, "tz": "America/Los_Angeles"},
    "KATL": {"lat": 33.6407, "lon": -84.4277, "tz": "America/New_York"},
}
FIXTURES = [
    {"id":"ordinary_2024_01_15", "target":"2024-01-15", "run":"2024-01-14T00:00:00Z", "case":"ordinary"},
    {"id":"dst_spring_2024_03_10", "target":"2024-03-10", "run":"2024-03-09T00:00:00Z", "case":"dst_spring"},
    {"id":"dst_fall_2024_11_03", "target":"2024-11-03", "run":"2024-11-02T00:00:00Z", "case":"dst_fall"},
    {"id":"historical_2025_11_15", "target":"2025-11-15", "run":"2025-11-14T00:00:00Z", "case":"historical"},
    {"id":"missing_expected_2020_01_15", "target":"2020-01-15", "run":"2020-01-14T00:00:00Z", "case":"expected_missing"},
]

def sha(b: bytes) -> str: return hashlib.sha256(b).hexdigest()
def canon(x) -> bytes: return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
def iso(dt): return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def dtz(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
def fixture_path(fid): return next(x for x in FIXTURES if x["id"] == fid)
def object_name(run, member, lead):
    d = dtz(run); ds=d.strftime('%Y%m%d'); hh=f"{lead:03d}"
    return ds, f"{member}.t00z.pgrb2a.0p50.f{hh}"
def url_for(run, member, lead):
    ds, n=object_name(run,member,lead)
    return f"{BASE}/gefs.{ds}/00/atmos/pgrb2ap5/{n}"
def idx_url_for(run, member, lead): return url_for(run,member,lead)+".idx"

def request(url, method="GET", headers=None):
    req=urllib.request.Request(url, method=method, headers=headers or {"User-Agent":"gefs-temperature-pilot/1.0"})
    started=datetime.now(timezone.utc)
    # These are deliberately independent of the wall-clock receipt fields.
    # Capture immediately before/after the actual network call so second-level
    # UTC formatting cannot create false evidence of serialized spacing.
    boundary_started_monotonic_ns=time.monotonic_ns()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body=r.read() if method != "HEAD" else b""
            h={k.lower():v for k,v in r.headers.items()}
            boundary_ended_monotonic_ns=time.monotonic_ns()
            return {"status":r.status,"headers":h,"body":body,"error":None,"started":iso(started),"ended":iso(datetime.now(timezone.utc)),"boundary_started_monotonic_ns":boundary_started_monotonic_ns,"boundary_ended_monotonic_ns":boundary_ended_monotonic_ns}
    except urllib.error.HTTPError as e:
        # urllib raises HTTPError for 4xx/5xx. Preserve the complete response
        # headers (including S3 Last-Modified when supplied) for fail-closed
        # expected-missing evidence rather than reducing it to status only.
        boundary_ended_monotonic_ns = time.monotonic_ns()
        headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return {"status": e.code, "headers": headers, "body": b"", "error": f"{type(e).__name__}: {e}", "started": iso(started), "ended": iso(datetime.now(timezone.utc)), "boundary_started_monotonic_ns": boundary_started_monotonic_ns, "boundary_ended_monotonic_ns": boundary_ended_monotonic_ns}
    except Exception as e:
        boundary_ended_monotonic_ns=time.monotonic_ns()
        return {"status":getattr(e,"code",None),"headers":{},"body":b"","error":f"{type(e).__name__}: {e}","started":iso(started),"ended":iso(datetime.now(timezone.utc)),"boundary_started_monotonic_ns":boundary_started_monotonic_ns,"boundary_ended_monotonic_ns":boundary_ended_monotonic_ns}

def write_json(p, obj): p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(canon(obj)+b"\n")
def load_manifest(out):
    p=out/'manifests'/'request_manifest.json'; return json.loads(p.read_text()) if p.exists() else {"requests":[],"failed":[]}

def acquire(out, prior=None, retry_version=None, recovery_f057=False, fixtures=None):
    out.mkdir(parents=True,exist_ok=True)
    fixtures = FIXTURES if fixtures is None else tuple(fixtures)
    frozen_fixture = {"version":"gefs-temperature-pilot-v1.0-2026-08-15","stations":STATIONS,"members":MEMBERS,"leads":LEADS,"fixtures":fixtures,"delay_seconds":DELAY}
    fixture_doc = dict(frozen_fixture)
    if retry_version:
        fixture_doc.update({"retry_version":retry_version,"timing_contract":{"clock":"time.monotonic_ns","boundary":"immediately before urlopen through response/body completion","minimum_consecutive_gap_seconds":MIN_REQUEST_GAP_SECONDS,"fail_closed":True}})
        if prior and (prior/'fixtures.json').exists():
            fixture_doc["prior_fixtures_sha256"] = sha((prior/'fixtures.json').read_bytes())
    write_json(out/'fixtures.json', fixture_doc)
    m=load_manifest(out); last=0
    timing_path=out/'manifests'/'timing_receipt.jsonl'
    timing_records=[]
    if timing_path.exists():
        timing_records=[json.loads(x) for x in timing_path.read_text().splitlines() if x.strip()]
    prior_by_url={}
    if prior and (prior/'manifests'/'request_manifest.json').exists():
        pm=json.loads((prior/'manifests'/'request_manifest.json').read_text())
        for x in pm.get('requests',[]):
            prior_by_url[x['url']] = x
        for x in pm.get('failed',[]):
            prior_by_url[x['url']] = x
    prior_sequence=[]
    if prior:
        for x in json.loads((prior/'manifests'/'request_manifest.json').read_text()).get('requests',[]) + json.loads((prior/'manifests'/'request_manifest.json').read_text()).get('failed',[]):
            prior_sequence.append({k:x.get(k) for k in ('fixture_id','member_id','lead','kind','url')})
    expected_sequence=[]
    for fx in fixtures:
      for member in MEMBERS:
       # Recovery mode is deliberately limited to the exact predeclared f057
       # boundary set for included fixtures. It never retries expected-missing.
       if recovery_f057:
         if fx['case'] == 'expected_missing':
           continue
         fixture_leads = (57,)
         fixture_members = MEMBERS
       else:
         # The deliberately missing fixture is a single predeclared route probe;
         # do not spend a full object budget on an already expected fail-closed case.
         fixture_leads = (24,) if fx['case'] == 'expected_missing' else (0,)+LEADS
         fixture_members = ('gec00',) if fx['case'] == 'expected_missing' else MEMBERS
       if member not in fixture_members: continue
       for lead in fixture_leads:
        u=url_for(fx['run'],member,lead); iu=idx_url_for(fx['run'],member,lead)
        for kind,url in (("grib2",u),("idx",iu)):
          expected_sequence.append({"fixture_id":fx['id'],"member_id":member,"lead":lead,"kind":kind,"url":url})
          # Resume safely: an already hash-recorded successful request is immutable
          # and is not re-fetched or overwritten on a continuation run.
          if any(x.get('url') == url and x.get('status') == 200 and x.get('sha256') for x in m['requests']):
            continue
          wait=DELAY-(time.monotonic()-last)
          if wait>0: time.sleep(wait)
          rec=request(url); last=time.monotonic()
          ent={"fixture_id":fx['id'],"member_id":member,"lead":lead,"kind":kind,"url":url,"retrieved_at_utc":rec['ended'],"request_started_utc":rec['started'],"request_ended_utc":rec['ended'],"status":rec['status'],"response_headers":rec['headers'],"error":rec['error']}
          previous_end_ns=timing_records[-1]['boundary_ended_monotonic_ns'] if timing_records else None
          gap_seconds=(rec['boundary_started_monotonic_ns']-previous_end_ns)/1_000_000_000 if previous_end_ns is not None else None
          timing_ent={"sequence":len(timing_records),"fixture_id":fx['id'],"member_id":member,"lead":lead,"kind":kind,"url":url,"method":"GET","status":rec['status'],"boundary_started_monotonic_ns":rec['boundary_started_monotonic_ns'],"boundary_ended_monotonic_ns":rec['boundary_ended_monotonic_ns'],"previous_boundary_ended_monotonic_ns":previous_end_ns,"inter_request_gap_seconds":gap_seconds,"gap_ok":gap_seconds is None or gap_seconds >= MIN_REQUEST_GAP_SECONDS}
          timing_records.append(timing_ent)
          timing_path.parent.mkdir(parents=True,exist_ok=True)
          timing_path.write_bytes(b''.join(canon(x)+b'\n' for x in timing_records))
          if gap_seconds is not None and gap_seconds < MIN_REQUEST_GAP_SECONDS:
            write_json(out/'manifests'/'timing_receipt.json', {"contract_minimum_gap_seconds":MIN_REQUEST_GAP_SECONDS,"request_count":len(timing_records),"minimum_gap_seconds":min(x['inter_request_gap_seconds'] for x in timing_records[1:] if x['inter_request_gap_seconds'] is not None),"status":"TIMING_BLOCKED","violation":timing_ent})
            raise RuntimeError(f"SERIALIZED_REQUEST_TIMING_VIOLATION:{gap_seconds:.9f}s<{MIN_REQUEST_GAP_SECONDS:.1f}s")
          if url in prior_by_url:
            ent["prior_status"] = prior_by_url[url].get("status")
            if prior_by_url[url].get("sha256"):
                ent["prior_sha256"] = prior_by_url[url]["sha256"]
          if rec['status']==200:
            ent.update({"bytes":len(rec['body']),"sha256":sha(rec['body'])})
            ent["hash_matches_prior"] = (not ent.get("prior_sha256")) or ent["sha256"] == ent["prior_sha256"]
            rel=Path('objects')/fx['id']/member
            if kind=='grib2': name=object_name(fx['run'],member,lead)[1]
            else: name=object_name(fx['run'],member,lead)[1]+'.idx'
            (out/rel).mkdir(parents=True,exist_ok=True); (out/rel/name).write_bytes(rec['body']); ent['path']=str(rel/name)
            if kind=='idx':
              lines=rec['body'].decode('utf-8','replace').splitlines()
              ent['idx_tmp_2m_lines']=[x for x in lines if ':TMP:2 m above ground:' in x]
            if kind=='grib2' and not rec['body'].startswith(b'GRIB'): ent['error']='not_grib2_magic'
            m['requests'].append(ent)
          else:
            m['failed'].append(ent)
          write_json(out/'manifests'/'request_manifest.json',m)
    sequence_doc={"version":retry_version or "gefs-temperature-pilot-request-set-v1","request_count":len(expected_sequence),"sequence":expected_sequence,"sequence_sha256":sha(canon(expected_sequence)),"prior_sequence_sha256":sha(canon(prior_sequence)) if prior_sequence else None,"matches_prior_sequence":(not prior_sequence) or expected_sequence == prior_sequence}
    write_json(out/'manifests'/'request_set_manifest.json',sequence_doc)
    gaps=[x['inter_request_gap_seconds'] for x in timing_records if x['inter_request_gap_seconds'] is not None]
    write_json(out/'manifests'/'timing_receipt.json',{"version":retry_version or "gefs-temperature-pilot-timing-v1","clock":"time.monotonic_ns","boundary":"immediately before urlopen through response/body completion","contract_minimum_gap_seconds":MIN_REQUEST_GAP_SECONDS,"request_count":len(timing_records),"minimum_gap_seconds":min(gaps) if gaps else None,"all_gaps_at_least_minimum":bool(gaps) and all(x >= MIN_REQUEST_GAP_SECONDS for x in gaps),"status":"TIMING_PASS" if gaps and all(x >= MIN_REQUEST_GAP_SECONDS for x in gaps) else "TIMING_BLOCKED"})
    return m

def main():
 p=argparse.ArgumentParser(); p.add_argument('--root',default=str(ROOT)); p.add_argument('--output'); p.add_argument('--retry-from'); p.add_argument('--retry-version'); p.add_argument('--recovery-f057',action='store_true'); p.add_argument('--acquire',action='store_true'); args=p.parse_args()
 out=Path(args.output) if args.output else Path(args.root)/'data/raw/gefs_temperature_pilot/2026-08-15'
 if args.acquire:
  prior=Path(args.retry_from) if args.retry_from else None
  m=acquire(out, prior=prior, retry_version=args.retry_version, recovery_f057=args.recovery_f057)
  print(json.dumps({'requests':len(m['requests']),'failed':len(m['failed']),'out':str(out)},sort_keys=True))
 else: print(out)
if __name__=='__main__': main()
