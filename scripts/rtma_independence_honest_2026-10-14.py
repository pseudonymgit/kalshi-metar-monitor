#!/usr/bin/env python3
"""
RTMA / om_gfs independence — the test done RIGHT (2026-10-14).

Dan's challenge (2026-10-14): the "0.96 correlated -> not a second degree of
freedom" conclusion was drawn from level correlation (meaningless — all temps
correlate) and/or thin 7-day windows with shared synoptic error. Design and run
an honest independence test on a LONGER window and on the RIGHT metric.

This script re-scores the stored RTMA probe rows (no re-fetch) and reports, for
each available probe window:

  1. fcerr as the study defines it (level Pearson, pooled + within-station demeaned)
  2. TEMPERATURE-DOMAIN error complementarity: corr of (mu - settled) residuals,
     pooled + demeaned, and the error-sign DISAGREEMENT rate (0.5 = independent)
  3. PROBABILITY-domain residual correlation (what the fusion consumes) — and a
     note when it is degenerate (all residuals share one sign -> no contrast)
  4. Murphy Brier decomposition (REL / RES / UNC) for rate_1h, RTMA, and a 50/50 blend
  5. Complementarity: on the days rate_1h misclassifies, how often does RTMA get it right?
  6. DECISION RULE (Dan's): RTMA is a useful second member iff
       (a) fcerr < 0.85  AND  (b) residual (temp-domain) corr < 0.7.

Read-only. Uses artifacts/rtma-scope/rtma_probe_*.json only.

Usage:
  python3 scripts/rtma_independence_honest_2026-10-14.py [probe.json ...]
"""
from __future__ import annotations

import glob
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "artifacts" / "rtma-scope"
FCERR_FLAG = 0.85
RESID_FLAG = 0.70


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def demean(vals, keys):
    g = {}
    for k, v in zip(keys, vals):
        g.setdefault(k, []).append(v)
    m = {k: sum(v) / len(v) for k, v in g.items()}
    return [v - m[k] for k, v in zip(keys, vals)]


def rmse(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)) / len(a))


def murphy(ps, ys, nbins=10):
    n = len(ps)
    base = sum(ys) / n
    bins = {}
    for p, y in zip(ps, ys):
        bins.setdefault(min(int(p * nbins), nbins - 1), []).append((p, y))
    rel = res = 0.0
    for rows in bins.values():
        nk = len(rows)
        pk = sum(r[0] for r in rows) / nk
        yk = sum(r[1] for r in rows) / nk
        rel += nk * (pk - yk) ** 2
        res += nk * (yk - base) ** 2
    rel /= n
    res /= n
    unc = base * (1 - base)
    bs = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    return dict(bs=round(bs, 4), rel=round(rel, 4), res=round(res, 4),
                unc=round(unc, 4), ident_resid=round(rel - res + unc - bs, 5))


def analyse(pr):
    w = pr["window"]
    rows = pr["rows"]
    print("=" * 78)
    print(f"WINDOW {w[0]} .. {w[1]}   station-days={len(rows)}  cycles={pr.get('rtma_cycles')}")
    out = {"window": w, "n_rows": len(rows)}

    for tag, key, fk in [("rtma_pin", "rtma_pin_p", "rtma_pin_F"),
                         ("rtma_hsf", "rtma_hsf_p", "rtma_hsf_F")]:
        ov = [r for r in rows
              if r["rate_1h_p"] is not None and r.get(key) is not None
              and r.get("rate_at_pin") is not None and r.get(fk) is not None]
        if len(ov) < 5:
            print(f"  {tag}: insufficient overlap ({len(ov)})")
            continue
        keys = [r["station"] for r in ov]
        y = [r["y"] for r in ov]

        # 1. study's fcerr (level)
        lvl_rate = [r["hsf_1330"] + r["rate_at_pin"] for r in ov]
        lvl_t = [r[fk] for r in ov]
        fcerr_pooled = pearson(lvl_rate, lvl_t)
        fcerr_dem = pearson(demean(lvl_rate, keys), demean(lvl_t, keys))

        # 2. temperature-domain residuals
        settled = [r["settled"] for r in ov]
        er = [a - b for a, b in zip(lvl_rate, settled)]
        et = [a - b for a, b in zip(lvl_t, settled)]
        resid_pooled = pearson(er, et)
        resid_dem = pearson(demean(er, keys), demean(et, keys))
        sign_dis = sum(1 for a, b in zip(er, et) if (a > 0) != (b > 0)) / len(er)

        # 3. probability-domain residual corr (fusion domain) + degeneracy check
        pr_ = [r["rate_1h_p"] - r["y"] for r in ov]
        pt = [r[key] - r["y"] for r in ov]
        pcross = pearson(pr_, pt)
        signs = {(a > 0) for a in pr_}
        degenerate = len(signs) <= 1

        # 4. Murphy
        mr = murphy([r["rate_1h_p"] for r in ov], y)
        mt = murphy([r[key] for r in ov], y)
        mb = murphy([(r["rate_1h_p"] + r[key]) / 2 for r in ov], y)

        # 5. complementarity
        wrong = [r for r in ov if (r["rate_1h_p"] > 0.5) != bool(r["y"])]
        right = [r for r in ov if (r["rate_1h_p"] > 0.5) == bool(r["y"])]
        help_rate = (sum(1 for r in wrong if (r[key] > 0.5) == bool(r["y"])) / len(wrong)
                     if wrong else float("nan"))

        ok_a = fcerr_dem < FCERR_FLAG
        ok_b = resid_dem < RESID_FLAG
        print(f"  -- {tag} (n={len(ov)})")
        print(f"     1. fcerr(level)      pooled={fcerr_pooled:.3f}  demeaned={fcerr_dem:.3f}")
        print(f"     2. resid corr (temp) pooled={resid_pooled:.3f}  demeaned={resid_dem:.3f}  "
              f"sign-DISAGREE={sign_dis:.3f} (0.5=max indep)")
        print(f"     3. pcross resid corr ={pcross:.3f}" + ("  [DEGENERATE: single-sign residuals]" if degenerate else ""))
        print(f"     4. Murphy  rate: BS={mr['bs']} REL={mr['rel']} RES={mr['res']} | "
              f"{tag}: BS={mt['bs']} REL={mt['rel']} RES={mt['res']} | 50/50: BS={mb['bs']}")
        print(f"     5. rate_1h wrong on {len(wrong)}; {tag} rescues {help_rate:.2%}; "
              f"rate_1h right on {len(right)}")
        print(f"     6. DECISION  (a) fcerr<0.85 -> {ok_a}   (b) resid<0.70 -> {ok_b}   "
              f"=> {'WIRE' if (ok_a and ok_b) else 'DO_NOT_WIRE'}")
        out[tag] = dict(n=len(ov), fcerr_pooled=round(fcerr_pooled, 4),
                        fcerr_demeaned=round(fcerr_dem, 4),
                        resid_pooled=round(resid_pooled, 4),
                        resid_demeaned=round(resid_dem, 4),
                        sign_disagree=round(sign_dis, 4), pcross_resid=round(pcross, 4),
                        murphy_rate=mr, murphy_rtma=mt, murphy_blend=mb,
                        rescue_rate=None if help_rate != help_rate else round(help_rate, 4),
                        criterion_a_fcerr_lt_085=ok_a, criterion_b_resid_lt_070=ok_b,
                        verdict="WIRE" if (ok_a and ok_b) else "DO_NOT_WIRE")
    return out


def main():
    args = sys.argv[1:]
    files = args or sorted(glob.glob(str(BASE / "rtma_probe_*.json")))
    if not files:
        print("no probe files found"); return
    results = {}
    for f in files:
        pr = json.loads(Path(f).read_text())
        results[Path(f).name] = analyse(pr)
    outp = BASE / "rtma_independence_honest_2026-10-14.json"
    outp.write_text(json.dumps(results, indent=2))
    print("\nwrote", outp)


if __name__ == "__main__":
    main()
