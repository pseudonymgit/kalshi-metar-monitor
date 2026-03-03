#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

KEYWORDS=[r"\bexecution_domain\b",r"\bmixed\b",r"\bguard\b",r"\brejection\b",r"\bforbidden\b",r"\bhydration\b",r"\bcache_valid\b",r"\bcache_missing\b",r"\bcache_age\b","ladder cache",r"\bseries_discovered\b","discovery mismatch",r"\bwatchlist\b","unknown market",r"\bstalled\b",r"\blag\b",r"\bscheduler\b",r"\bsettlement_up\b",r"\breversion_after_settlement\b","alert sent",r"\bsuppressed\b",r"\btransition_without_alert\b"]
ENDPOINTS=["/execution-domain","/observability/ingestion-health","/observability/hydration-prerequisite-runtime","/observability/runtime-authority-snapshot","/observability/alert-fire-audit","/observability/transitions","/observability/station-summary","/observability/market-coverage","/metar/watchlist"]

def files(root: Path):
    out=[]
    for d in [root/"logs",root/"docs",root/"evidence_out"]:
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and p.suffix.lower() in {'.log','.out','.txt','.md'}:
                    out.append(p)
    tmp=Path('/tmp')
    if tmp.is_dir():
        for p in tmp.glob('kalshi*'):
            if p.is_file() and p.suffix.lower() in {'.log','.out','.txt'}:
                out.append(p)
    return sorted(set(out))

def scan(path, regs):
    c=Counter(); hits=[]
    try:
        for i,l in enumerate(path.open(encoding='utf-8',errors='replace'),1):
            ll=l.lower(); m=[]
            for r in regs:
                if r.search(ll): c[r.pattern]+=1; m.append(r.pattern)
            if m and len(hits)<100:
                hits.append({"file":str(path),"line":i,"matched":','.join(m),"text":l.strip()})
    except Exception as e:
        hits.append({"file":str(path),"line":0,"matched":"error","text":str(e)})
    return c,hits

def endpoints(base_url: str, timeout_s: float):
    base=base_url.rstrip('/')
    snaps={}
    failed=0
    for ep in ENDPOINTS:
        url=f"{base}{ep}"
        req=request.Request(url, method="GET")
        try:
            with request.urlopen(req, timeout=timeout_s) as resp:
                raw=resp.read().decode('utf-8','replace')
                try: body=json.loads(raw)
                except Exception: body=raw[:4000]
                snaps[ep]={"status":resp.status,"body":body}
        except Exception as e:
            failed += 1
            if isinstance(e, error.HTTPError):
                raw=e.read().decode('utf-8','replace')
                try: body=json.loads(raw)
                except Exception: body=raw[:4000]
                snaps[ep]={"status":e.code,"body":body}
            else:
                snaps[ep]={"error":str(e),"curl":f"curl -sS {base}{ep}"}
    if failed == len(ENDPOINTS):
        snaps["curl_fallback"]=[f"curl -sS {base}{ep}" for ep in ENDPOINTS]
    return snaps

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out-dir',default='evidence_out'); ap.add_argument('--base-url',default='http://127.0.0.1:5000'); ap.add_argument('--timeout-s',type=float,default=3.0); a=ap.parse_args()
    out=Path(a.out_dir); out.mkdir(exist_ok=True)
    b=out/f"bundle_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"; b.mkdir(exist_ok=True)
    regs=[re.compile(k) for k in KEYWORDS]
    total=Counter(); sample=[]
    for f in files(Path.cwd()):
        c,h=scan(f,regs); total.update(c); sample.extend(h[:5])
    bundle={"collected_at_utc":datetime.now(timezone.utc).isoformat(),"cwd":os.getcwd(),"base_url":a.base_url,"keyword_counts":dict(total),"sample_hits":sample[:200],"endpoint_snapshots":endpoints(a.base_url,a.timeout_s)}
    p=b/'evidence_bundle.json'; p.write_text(json.dumps(bundle,indent=2,sort_keys=True,default=str),encoding='utf-8')
    print(p)

if __name__=='__main__': main()
