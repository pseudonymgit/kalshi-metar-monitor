#!/usr/bin/env python3
"""Official NCEI SRRS CLI extractor: .scan indexes + exact HTTP byte ranges only.

Read-only provenance acquisition. Never downloads an ANX bundle without a Range header.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, os, re, sys, time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://www.ncei.noaa.gov/data/service-records-retention-system/access/anx"
UA = "OpenClaw official NCEI SRRS CLI historical backfill/1.0 (weather-engine provenance QC)"
START_DEFAULT, END_DEFAULT = date(2025, 1, 15), date(2026, 2, 28)
RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}
# Exact frozen 20-station contract (also checked against data/station_mapping.json when present).
STATIONS = {
 "KATL": ("FFC", "ATL", "CLIATL", "KFFC", "KXHIGHTATL"), "KAUS": ("EWX", "AUS", "CLIAUS", "KEWX", "KXHIGHAUS"),
 "KBOS": ("BOX", "BOS", "CLIBOS", "KBOX", "KXHIGHTBOS"), "KDCA": ("LWX", "DCA", "CLIDCA", "KLWX", "KXHIGHTDC"),
 "KDEN": ("BOU", "DEN", "CLIDEN", "KBOU", "KXHIGHDEN"), "KDFW": ("FWD", "DFW", "CLIDFW", "KFWD", "KXHIGHTDAL"),
 "KHOU": ("HGX", "HOU", "CLIHOU", "KHGX", "KXHIGHTHOU"), "KLAS": ("VEF", "LAS", "CLILAS", "KVEF", "KXHIGHTLV"),
 "KLAX": ("LOX", "LAX", "CLILAX", "KLOX", "KXHIGHLAX"), "KMDW": ("LOT", "MDW", "CLIMDW", "KLOT", "KXHIGHCHI"),
 "KMIA": ("MFL", "MIA", "CLIMIA", "KMFL", "KXHIGHTMIA"), "KMSP": ("MPX", "MSP", "CLIMSP", "KMPX", "KXHIGHTMIN"),
 "KMSY": ("LIX", "MSY", "CLIMSY", "KLIX", "KXHIGHTNOLA"), "KNYC": ("OKX", "NYC", "CLINYC", "KOKX", "KXHIGHNY"),
 "KOKC": ("OUN", "OKC", "CLIOKC", "KOUN", "KXHIGHTOKC"), "KPHL": ("PHI", "PHL", "CLIPHL", "KPHI", "KXHIGHPHIL"),
 "KPHX": ("PSR", "PHX", "CLIPHX", "KPSR", "KXHIGHTPHX"), "KSAT": ("EWX", "SAT", "CLISAT", "KEWX", "KXHIGHTSATX"),
 "KSEA": ("SEW", "SEA", "CLISEA", "KSEW", "KXHIGHTSEA"), "KSFO": ("MTR", "SFO", "CLISFO", "KMTR", "KXHIGHTSFO"),
}


def sha(b: bytes) -> str: return hashlib.sha256(b).hexdigest()
def iso() -> str: return datetime.now(timezone.utc).isoformat()
def atomic_json(path: Path, obj: Any):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)

def safe_part(x: str) -> str: return re.sub(r"[^A-Za-z0-9_.-]", "_", x)

def parse_summary_date(text: str) -> str | None:
    pats = [
      r"SUMMARY FOR [A-Z]+\s+(\d{1,2})\s+(\d{4})", r"SUMMARY FOR ([A-Z]+)\s+(\d{1,2})\s+(\d{4})",
      r"SUMMARY FOR\s+(\w+)\s+(\d{1,2})\s+(\d{4})",
    ]
    months = {m.upper(): i for i,m in enumerate(["January","February","March","April","May","June","July","August","September","October","November","December"],1)}
    # archive reports use e.g. JANUARY 15 2025; allow arbitrary station phrase before month.
    m = re.search(r"SUMMARY FOR .*?\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2})\s+(\d{4})", text, re.S)
    if not m: return None
    try: return date(int(m.group(3)), months[m.group(1)], int(m.group(2))).isoformat()
    except ValueError: return None

def parse_temp(text: str, which: str) -> int | None:
    # Restrict to the TEMPERATURE section and its TODAY/YESTERDAY table row.
    sec = text[text.find("TEMPERATURE (F)"):] if "TEMPERATURE (F)" in text else text
    # Require the row to be in the first daily table (before precipitation),
    # so a later normals/record line cannot be mistaken for the observation.
    sec = sec.split("PRECIPITATION", 1)[0]
    m = re.search(rf"^\s*{which}\s+(-?\d+|MM)\b", sec, re.M)
    if not m or m.group(1) == "MM": return None
    return int(m.group(1))

def report_classification(text: str) -> dict[str, Any]:
    """Classify report semantics from the bulletin body, never from file metadata.

    ``base_class`` is the observation-period class used by selection.  Revision
    and duplicate-content are provenance labels because either can coexist with
    an intermediate/full-day semantic class.
    """
    upper = text.upper()
    # The first temperature table/record paragraph is authoritative.  A full
    # day report can mention TODAY later in its normals section.
    temp = upper[upper.find("TEMPERATURE (F)"):] if "TEMPERATURE (F)" in upper else upper
    temp = temp.split("PRECIPITATION", 1)[0]
    has_yesterday = bool(re.search(r"^\s*YESTERDAY\s*$", temp, re.M))
    has_today = bool(re.search(r"^\s*TODAY\s*$", temp, re.M))
    has_valid_as_of = bool(re.search(r"VALID (?:AS OF|TODAY AS OF)", upper))
    if has_yesterday:
        base = "full_day"
    elif has_today and has_valid_as_of:
        base = "intermediate"
    elif has_today:
        base = "intermediate"
    else:
        base = "unresolved"
    # Keep this deliberately narrow: ordinary narrative uses of these words
    # must not turn a report into a revision without an explicit marker.
    revision = bool(re.search(
        r"(?:\bCORRECT(?:ED|ION)?\b|\bREVISED\b|\bREVISION\b|\bAMENDED\b|\bAMENDMENT\b|\bUPDATED\b)",
        upper,
    ))
    return {"base_class": base, "revision": revision, "has_today": has_today,
            "has_yesterday": has_yesterday, "has_valid_as_of": has_valid_as_of}


def classify(text: str) -> str:
    """Compatibility wrapper returning the semantic class."""
    return report_classification(text)["base_class"]


def select_lineage(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Select one coherent full-day report while retaining all provenance.

    The input may contain repeated occurrences of identical raw content.  Such
    occurrences are not conflicts.  Conflicts are distinct full-day settlement
    values for the same station/date.
    """
    reports = [dict(r) for r in reports]
    for r in reports:
        r.setdefault("base_class", r.get("class"))
        labels = list(r.get("lineage_labels", []))
        if r.get("revision") and "revision" not in labels:
            labels.append("revision")
        r["lineage_labels"] = labels
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for r in reports:
        by_hash.setdefault(r.get("raw_sha256", ""), []).append(r)
    for group in by_hash.values():
        for i, r in enumerate(sorted(group, key=lambda x: (x.get("raw_path", ""), x.get("scan_url", "")))):
            if i:
                if "duplicate_content" not in r["lineage_labels"]:
                    r["lineage_labels"].append("duplicate_content")
            r["duplicate_content"] = len(group) > 1
    full = [r for r in reports if r.get("base_class", r.get("class")) == "full_day"]
    intermediates = [r for r in reports if r.get("base_class", r.get("class")) == "intermediate"]
    result: dict[str, Any] = {
        "reports": reports, "selected": None, "status": "blocked_unresolved_lineage",
        "full_day_count": len(full), "intermediate_count": len(intermediates),
        "duplicate_content_count": sum(1 for r in reports if "duplicate_content" in r["lineage_labels"]),
        "conflict_count": 0,
    }
    if not full:
        unresolved = [r for r in reports if r.get("base_class", r.get("class")) == "unresolved"]
        result["status"] = ("blocked_unresolved_lineage" if unresolved else
                             ("blocked_no_full_day" if intermediates else "missing_no_cli"))
        return result
    values = {(r.get("max_f"), r.get("min_f")) for r in full}
    if len(values) > 1:
        result["status"] = "blocked_conflicting_full_day_revisions"
        result["conflict_count"] = len(values)
        return result
    # A stable max issuance header is preferable, with hash/path as a tie-break.
    selected = sorted(full, key=lambda r: (r.get("issuance_header", ""), r.get("raw_sha256", ""), r.get("raw_path", "")))[-1]
    selected["settlement_selected"] = True
    for r in reports:
        if r is not selected:
            r["settlement_selected"] = False
    result["selected"] = selected
    result["status"] = "accepted_full_day"
    return result

def parse_row(line: str) -> dict[str, Any] | None:
    try:
        parts = next(csv.reader([line]))
        if len(parts) < 4: return None
        fn, start, length, wmo = (x.strip() for x in parts[:4])
        if not start.isdigit() or not length.isdigit(): return None
        return {"filename": fn, "start": int(start), "length": int(length), "wmo": wmo, "row": line.rstrip("\r\n")}
    except Exception: return None

class Client:
    def __init__(self, out: Path, delay: float = .2, timeout: float = 60, retries: int = 5):
        self.s = requests.Session(); self.s.headers.update({"User-Agent": UA, "Accept": "*/*"})
        self.out, self.delay, self.timeout, self.retries = out, delay, timeout, retries
        self.last = 0.0
        self.attempts = out / "attempts.jsonl"
    def request(self, url: str, headers: dict[str,str] | None = None, cache: Path | None = None) -> tuple[bytes|None, dict[str,Any]]:
        if cache and cache.exists():
            b = cache.read_bytes(); meta_path = cache.with_suffix(cache.suffix + ".meta.json")
            old = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            return b, {"url": url, "status": "cache", "http_status": old.get("http_status", 200), "bytes": len(b), "sha256": sha(b), "content_range": old.get("content_range")}
        last_err = None
        for n in range(self.retries + 1):
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0: time.sleep(wait)
            try:
                r = self.s.get(url, headers=headers or {}, timeout=self.timeout)
                self.last = time.monotonic()
                meta = {"url": url, "attempt": n+1, "http_status": r.status_code, "bytes": len(r.content),
                        "content_range": r.headers.get("Content-Range"), "content_type": r.headers.get("Content-Type"), "retrieved_at_utc": iso()}
                if r.status_code == 200 or r.status_code == 206:
                    if cache:
                        cache.parent.mkdir(parents=True, exist_ok=True); cache.write_bytes(r.content)
                        cache.with_suffix(cache.suffix + ".meta.json").write_text(json.dumps(meta, sort_keys=True))
                    meta["sha256"] = sha(r.content); return r.content, meta
                if r.status_code not in RETRY_CODES: return None, meta | {"error": "permanent_http"}
                ra = r.headers.get("Retry-After")
                sleep = float(ra) if ra and ra.isdigit() else min(2 ** n, 120)
                time.sleep(sleep); last_err = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                self.last = time.monotonic(); last_err = repr(e); time.sleep(min(2 ** n, 120))
        return None, {"url": url, "attempt": self.retries + 1, "status": "error", "error": last_err, "retrieved_at_utc": iso()}
    def log(self, meta: dict[str,Any]):
        with self.attempts.open("a") as f: f.write(json.dumps(meta, sort_keys=True) + "\n")

def listing_links(html: bytes, base: str) -> list[str]:
    text = html.decode("utf-8", "replace")
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', text, re.I)
    return sorted({urljoin(base, h) for h in hrefs if h.lower().endswith(".scan")})

def check_range(meta: dict, start: int, length: int, body: bytes) -> str | None:
    if meta.get("http_status") != 206: return "range_not_206"
    if len(body) != length: return "range_length_mismatch"
    cr = meta.get("content_range") or ""
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", cr)
    if not m or int(m.group(1)) != start or int(m.group(2)) != start + length - 1: return "content_range_mismatch"
    return None

def dates(a: date, b: date):
    while a <= b: yield a; a += timedelta(days=1)

def station_keys(stations: list[str], a: date, b: date):
    for d in dates(a,b):
        for st in stations: yield st, d

def proof(out: Path, client: Client):
    st, target = "KATL", date(2025,1,15); office, issued, pil, wmo, _ = STATIONS[st]
    scan_url = f"{BASE}/2025/01/16/01/2025011601_SRRS0026.ANX.scan"
    scan_path = out / "scans" / "proof_2025011601_SRRS0026.ANX.scan"
    b, meta = client.request(scan_url, cache=scan_path); client.log(meta | {"kind":"proof_scan"})
    if b is None: raise RuntimeError("proof scan request failed")
    rows = [parse_row(x.decode("utf-8", "replace")) for x in b.splitlines()[1:]]
    # A single WMO header is repeated for each CLI station in this bundle;
    # station identity is proven by the PIL in the extracted body.
    row = next((r for r in rows if r and r["wmo"].startswith("CDUS42 KFFC 160132")
                and r["length"] == 4407 and r["start"] == 14856553), None)
    if not row: raise RuntimeError("proof scan row not found")
    url = f"{BASE}/2025/01/16/01/{row['filename']}"
    raw_path = out / "raw" / "proof_KATL_2025-01-15.raw"
    body, rm = client.request(url, {"Range": f"bytes={row['start']}-{row['start']+row['length']-1}"})
    client.log(rm | {"kind":"proof_range", "range": row})
    if body is None: raise RuntimeError("proof range request failed")
    err = check_range(rm,row["start"],row["length"],body)
    if err: raise RuntimeError(err)
    raw_path.parent.mkdir(parents=True,exist_ok=True); raw_path.write_bytes(body)
    text = body.decode("ascii", "replace"); result={"station":st,"target_date":target.isoformat(),"scan_url":scan_url,"scan_sha256":sha(b),"row":row,"raw_url":url,"raw_bytes":len(body),"raw_sha256":sha(body),"internal_summary_date":parse_summary_date(text),"pil_match":bool(re.search(r"\bCLIATL\b", text[:300])),"max_f":parse_temp(text,"MAXIMUM"),"min_f":parse_temp(text,"MINIMUM"),"class":classify(text),"expected_raw_sha256":"6eaa594496635d54a80e1323a45f255f971d04195dfcb9432825c24f1f4a05b4","pass":sha(body)=="6eaa594496635d54a80e1323a45f255f971d04195dfcb9432825c24f1f4a05b4" and parse_summary_date(text)==target.isoformat() and parse_temp(text,"MAXIMUM")==52 and parse_temp(text,"MINIMUM")==34 and bool(re.search(r"\bCLIATL\b", text[:300]))}
    atomic_json(out/"proof_qc.json",result); return result

def run(args):
    a=date.fromisoformat(args.start); b=date.fromisoformat(args.end); stations=args.stations.split(",") if args.stations else list(STATIONS)
    bad=set(stations)-set(STATIONS)
    if bad: raise SystemExit(f"unknown station(s): {sorted(bad)}")
    out=Path(args.output or ROOT/"data/raw/srrs_cli_backfill/2026-08-15"); out.mkdir(parents=True,exist_ok=True)
    for d in ("scans","raw","reports","failures"): (out/d).mkdir(exist_ok=True)
    c=Client(out,args.delay,args.timeout,args.retries)
    if args.proof: print(json.dumps(proof(out,c),sort_keys=True)); return
    ckpath=out/"checkpoint.json"; ck=json.loads(ckpath.read_text()) if ckpath.exists() else {"schema_version":1,"started_at_utc":iso(),"completed":{}}
    manifest=out/"manifest.jsonl"; completed=ck.setdefault("completed",{})
    summary={"schema_version":1,"source":BASE,"start":a.isoformat(),"end":b.isoformat(),"stations":stations,"attempted_keys":0,"found_keys":0,"missing_keys":0,"blocked_keys":0,"reports":0,"raw_bytes":0}
    for st,d in station_keys(stations,a,b):
        key=f"{st}|{d.isoformat()}"
        if key in completed and args.resume: continue
        office,issued,pil,wmo,_=STATIONS[st]; searches=[]; candidates=[]; failures=[]
        for sd in [d-timedelta(days=1),d,d+timedelta(days=1)]:
            daybase=f"{BASE}/{sd:%Y/%m/%d}"
            # Fetch day listing once, then only listed hour directories.
            daycache=out/"scans"/f"listing_{sd:%Y%m%d}.html"
            db,dm=c.request(daybase+"/",cache=daycache); c.log(dm|{"kind":"day_listing","target_key":key,"archive_date":sd.isoformat()})
            if db is None: failures.append({"date":sd.isoformat(),"kind":"day_listing","meta":dm}); continue
            hours=sorted(set(re.findall(r'href=["\']([0-9]{2})/["\']',db.decode("utf8","replace"),re.I)))
            if not hours: hours=[f"{h:02d}" for h in range(24)]
            for hh in hours:
                hb=f"{daybase}/{hh}"; links=listing_links(c.request(hb+"/",cache=out/"scans"/f"listing_{sd:%Y%m%d}_{hh}.html")[0] or b"",hb+"/")
                if not links:
                    # Listing call above is intentionally logged separately below only for failures.
                    continue
                for scan_url in links:
                    fn=scan_url.rsplit("/",1)[-1]; scanpath=out/"scans"/fn
                    sb,sm=c.request(scan_url,cache=scanpath); c.log(sm|{"kind":"scan","target_key":key})
                    if sb is None: failures.append({"scan_url":scan_url,"kind":"scan"}); continue
                    for rawline in sb.splitlines()[1:]:
                        r=parse_row(rawline.decode("utf8","replace"));
                        if not r or not r["wmo"].startswith("CDUS") or f" {wmo} " not in f" {r['wmo']} ": continue
                        # Ensure the WMO header is the office token, not a similarly named bulletin.
                        if wmo not in r["wmo"]: continue
                        rk={**r,"scan_url":scan_url,"archive_date":sd.isoformat(),"hour":hh}
                        candidates.append(rk)
                        au=scan_url[:-5] # remove .scan
                        range_hdr={"Range":f"bytes={r['start']}-{r['start']+r['length']-1}"}
                        body,rm=c.request(au,headers=range_hdr); c.log(rm|{"kind":"range","target_key":key,"range":r})
                        if body is None: failures.append({"kind":"range","row":r,"meta":rm}); continue
                        er=check_range(rm,r["start"],r["length"],body)
                        if er: failures.append({"kind":er,"row":r,"meta":rm}); continue
                        text=body.decode("ascii","replace")
                        if not re.search(rf"\b{re.escape(pil)}\b",text[:300]): continue
                        internal=parse_summary_date(text)
                        if internal != d.isoformat():
                            failures.append({"kind":"internal_date_mismatch","internal_summary_date":internal,"row":r,"sha256":sha(body)}); continue
                        rawname=f"{st}_{d.isoformat()}_{safe_part(fn)}_{r['start']}_{r['length']}_{sha(body)[:16]}.raw"; rawpath=out/"raw"/rawname
                        if rawpath.exists() and rawpath.read_bytes()!=body: raise RuntimeError(f"immutable raw collision: {rawpath}")
                        rawpath.write_bytes(body)
                        parsed_class = report_classification(text)
                        rec={"station":st,"target_date":d.isoformat(),"issuedby":issued,"pil":pil,"wmo_header":r["wmo"],"scan_url":scan_url,"scan_row":r["row"],"raw_path":str(rawpath.relative_to(out)),"raw_url":au,"start":r["start"],"length":r["length"],"raw_bytes":len(body),"raw_sha256":sha(body),"internal_summary_date":internal,"class":parsed_class["base_class"],"base_class":parsed_class["base_class"],"revision":parsed_class["revision"],"max_f":parse_temp(text,"MAXIMUM"),"min_f":parse_temp(text,"MINIMUM"),"issuance_header":text.splitlines()[0] if text.splitlines() else ""}
                        candidates[-1]["accepted"] = rec; summary["reports"]+=1; summary["raw_bytes"]+=len(body)
        accepted=[x["accepted"] for x in candidates if x.get("accepted")]
        lineage=select_lineage(accepted)
        accepted=lineage["reports"]
        status=lineage["status"] if accepted or not failures else ("blocked_archive_access" if not candidates else lineage["status"])
        rec={"schema_version":2,"key":key,"station":st,"target_date":d.isoformat(),"status":status,"searched_dates":[(d+timedelta(days=i)).isoformat() for i in (-1,0,1)],"candidate_rows":len(candidates),"accepted_reports":accepted,"selected_report":lineage.get("selected"),"lineage":{k:v for k,v in lineage.items() if k not in {"reports","selected"}},"failures":failures}
        with manifest.open("a") as f: f.write(json.dumps(rec,sort_keys=True)+"\n")
        completed[key]={"status":status,"manifest_line":sum(1 for _ in manifest.open())}
        summary["attempted_keys"]+=1; summary[status.split("_")[0]+"_keys"] = summary.get(status.split("_")[0]+"_keys",0)+1
        atomic_json(ckpath,ck)
        if summary["attempted_keys"]%10==0: print(json.dumps({"progress":summary["attempted_keys"],"last":key,"status":status}),flush=True)
    atomic_json(out/"run_summary.json",summary); print(json.dumps(summary,sort_keys=True))

def main():
    p=argparse.ArgumentParser(); p.add_argument("--output"); p.add_argument("--start",default=START_DEFAULT.isoformat()); p.add_argument("--end",default=END_DEFAULT.isoformat()); p.add_argument("--stations"); p.add_argument("--delay",type=float,default=.2); p.add_argument("--timeout",type=float,default=60); p.add_argument("--retries",type=int,default=5); p.add_argument("--resume",action="store_true"); p.add_argument("--proof",action="store_true"); run(p.parse_args())
if __name__ == "__main__": main()
