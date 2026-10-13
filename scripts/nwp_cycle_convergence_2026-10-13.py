#!/usr/bin/env python3
"""A1 — Hourly forecast convergence: do late NWP cycles beat early ones? (2026-10-13)

Question: as the day progresses (later model cycle), do successive NWP runs
converge on settlement truth? If a late-day cycle (18Z-00Z) beats an early-day
cycle (06Z-12Z) by >= 0.1 F MAE, cycle-age weighting is actionable.

Data: data/nwp_forecasts.db (vintages keyed by fetch_date + fetch_timestamp
hour) joined to canonical settlement truth (metar_backfill.db settlement_truth
HIGH, trustworthy=1). Read-only. No temp DBs.

Two designs are reported:
  (A) LITERAL (task spec): for each station-day, mean |forecast - truth| over
      all early-hour vintages vs all late-hour vintages; paired t-test on the
      station-days that have BOTH. NOTE: cycle phase is confounded with lead
      time here (late-hour fetches tend to be nearer the target).
  (B) LEAD-MATCHED (rigorous): pair early vs late vintage error at the SAME
      lead time (target_date - fetch_date), isolating cycle phase from lead.
  Plus (C) MAE vs lead time, the direct "convergence" view.

Output: artifacts/nwp-cycle-convergence/nwp_cycle_convergence_2026-10-13.{json,md}
"""
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NWP = REPO / "data" / "nwp_forecasts.db"
METAR = REPO / "data" / "metar_backfill.db"
ART = REPO / "artifacts" / "nwp-cycle-convergence"

EARLY_HOURS = {5, 6, 10, 12}          # 06Z / 12Z-ish runs
LATE_HOURS = {16, 17, 18, 22, 23, 0, 1}  # 18Z / 00Z-ish runs
SETTLE_END = date(2026, 10, 9)
EARLY_START = date(2026, 4, 1)


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def c2f(c):
    return None if c is None else c * 9.0 / 5.0 + 32.0


def load_truth():
    c = ro(METAR)
    out = {}
    for st, ds, v in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='HIGH' AND trustworthy=1 AND settlement_temp_f IS NOT NULL"):
        out[(st, ds)] = float(v)
    c.close()
    return out


def load_all():
    c = ro(NWP)
    vin = defaultdict(list)  # (st, td, model) -> [(lead, hour, phase, valueF)]

    for model in ("gfs", "gem", "icon"):
        for st, td, fd, hh, v in c.execute(
                "SELECT station, target_date, fetch_date, substr(fetch_timestamp,12,2), value "
                "FROM nwp_forecasts WHERE model=? AND variable='temperature_2m_max'", (model,)):
            if v is None:
                continue
            try:
                lead = (date.fromisoformat(td) - date.fromisoformat(fd)).days
                hh = int(hh)
            except (ValueError, TypeError):
                continue
            phase = "early" if hh in EARLY_HOURS else ("late" if hh in LATE_HOURS else None)
            if phase is None:
                continue
            vin[(st, td, model)].append((lead, hh, phase, c2f(v)))

    # gefs_ens: mean of gep members per (station,target,fetch_date), phase by fetch hour
    mem = defaultdict(lambda: defaultdict(list))
    for st, td, fd, hh, var, v in c.execute(
            "SELECT station, target_date, fetch_date, substr(fetch_timestamp,12,2), "
            "variable, value FROM nwp_forecasts WHERE model='gefs_ens' "
            "AND variable LIKE 'temperature_2m_max_member%'"):
        if v is None:
            continue
        mem[(st, td, fd)][hh].append((var, v))
    for (st, td, fd), hours in mem.items():
        try:
            lead = (date.fromisoformat(td) - date.fromisoformat(fd)).days
        except ValueError:
            continue
        for hh, pairs in hours.items():
            try:
                hh = int(hh)
            except (ValueError, TypeError):
                continue
            phase = "early" if hh in EARLY_HOURS else ("late" if hh in LATE_HOURS else None)
            if phase is None:
                continue
            vals = [c2f(v) for _, v in pairs]
            vin[(st, td, "gefs_ens")].append((lead, hh, phase, sum(vals) / len(vals)))
    c.close()
    return vin


def mean(xs):
    return sum(xs) / len(xs)


def std(xs, ddof=1):
    n = len(xs)
    if n <= ddof:
        return float("nan")
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def paired_t(es, ls):
    """Paired t on per-station-day (early_err, late_err). delta = early - late.
    Positive delta => late is closer (better). Returns t, p(2-sided, normal appx)."""
    d = [e - l for e, l in zip(es, ls)]
    n = len(d)
    md = mean(d)
    sd = std(d)
    se = sd / math.sqrt(n) if sd and sd > 0 else None
    t = md / se if se else None
    p = None
    if t is not None:
        p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return n, md, (se if se else None), (t if t else None), p


def analyze(truth, vin):
    models = ("gfs", "gem", "icon", "gefs_ens")
    out = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
           "window": [EARLY_START.isoformat(), SETTLE_END.isoformat()],
           "cycle_phase_def": {"early_hours": sorted(EARLY_HOURS),
                               "late_hours": sorted(LATE_HOURS)},
           "design_note": (
               "The archive stores ~one fetch per model per fetch_date, so fetch "
               "HOUR (cycle) is perfectly confounded with fetch DATE (lead time): "
               "an early-hour vintage is from an earlier day and therefore a longer "
               "lead. A lead-matched early-vs-late pairing is therefore structurally "
               "impossible in this archive (0 same-lead pairs). The literal per-"
               "station-day test (design A) compares phases at DIFFERENT leads and "
               "its sign is unreliable for a pure cycle-age question."),
           "models": {}}

    for model in models:
        # group vintages by station-day (only those with truth, in window)
        perday = defaultdict(list)
        for (st, td, m), vints in vin.items():
            if m != model:
                continue
            if (st, td) not in truth:
                continue
            try:
                d = date.fromisoformat(td)
            except ValueError:
                continue
            if d < EARLY_START or d > SETTLE_END:
                continue
            perday[(st, td)] = vints
        if not perday:
            continue

        # per station-day phase-mean abs error (literal design A)
        lit_rows = []   # (station,td, early_err, late_err)
        lead_rows = []  # (station,td,lead, early_err, late_err)  design B
        lead_mae = defaultdict(lambda: defaultdict(list))  # phase -> lead -> [err]
        n_vint = 0
        for (st, td), vints in perday.items():
            t = truth[(st, td)]
            byphase = defaultdict(list)
            byphase_lead = defaultdict(lambda: defaultdict(list))
            for lead, hh, phase, val in vints:
                err = abs(val - t)
                n_vint += 1
                byphase[phase].append(err)
                byphase_lead[phase][lead].append(err)
                lead_mae[phase][lead].append(err)
            if byphase.get("early") and byphase.get("late"):
                lit_rows.append((st, td, mean(byphase["early"]), mean(byphase["late"])))
            for lead in set(byphase_lead["early"]) & set(byphase_lead["late"]):
                lead_rows.append((st, td, lead,
                                  mean(byphase_lead["early"][lead]),
                                  mean(byphase_lead["late"][lead])))

        # literal MAE per phase (pooled over all vintages)
        early_all, late_all = [], []
        for (st, td), vints in perday.items():
            t = truth[(st, td)]
            for lead, hh, phase, val in vints:
                (early_all if phase == "early" else late_all).append(abs(val - t))

        lit_es = [r[2] for r in lit_rows]
        lit_ls = [r[3] for r in lit_rows]
        n_lit, md_lit, se_lit, t_lit, p_lit = paired_t(lit_es, lit_ls) if lit_rows else (0, None, None, None, None)
        ld_es = [r[3] for r in lead_rows]
        ld_ls = [r[4] for r in lead_rows]
        n_ld, md_ld, se_ld, t_ld, p_ld = paired_t(ld_es, ld_ls) if lead_rows else (0, None, None, None, None)

        mae_early = round(mean(early_all), 4) if early_all else None
        mae_late = round(mean(late_all), 4) if late_all else None
        lit_sig = (se_lit is not None and md_lit is not None and abs(md_lit) > 2 * se_lit)
        led_sig = (se_ld is not None and md_ld is not None and abs(md_ld) > 2 * se_ld)
        out["models"][model] = {
            "station_days": len(perday),
            "vintages": n_vint,
            "n_vintage_early": len(early_all),
            "n_vintage_late": len(late_all),
            "mae_early_pooled_all_vintages": mae_early,
            "mae_late_pooled_all_vintages": mae_late,
            "literal_paired": {
                "n_station_days_both": n_lit,
                "mae_early_stationdaymean": round(mean(lit_es), 4) if lit_es else None,
                "mae_late_stationdaymean": round(mean(lit_ls), 4) if lit_ls else None,
                "delta_early_minus_late_F": round(md_lit, 4) if md_lit is not None else None,
                "se": round(se_lit, 5) if se_lit else None,
                "t": round(t_lit, 3) if t_lit else None,
                "p_two_sided": round(p_lit, 5) if p_lit is not None else None,
                "late_better_by_ge_0.1F": (md_lit is not None and md_lit >= 0.1),
                "significant_2se": (se_lit is not None and md_lit is not None and abs(md_lit) > 2 * se_lit),
            },
            "lead_matched_paired": {
                "n_pairs": n_ld,
                "delta_early_minus_late_F": round(md_ld, 4) if md_ld is not None else None,
                "se": round(se_ld, 5) if se_ld else None,
                "t": round(t_ld, 3) if t_ld else None,
                "p_two_sided": round(p_ld, 5) if p_ld is not None else None,
                "late_better_by_ge_0.1F": (md_ld is not None and md_ld >= 0.1),
                "significant_2se": (se_ld is not None and md_ld is not None and abs(md_ld) > 2 * se_ld),
            },
            "mae_by_phase_by_lead": {
                ph: {str(l): round(mean(v), 4) for l, v in sorted(d.items())}
                for ph, d in lead_mae.items()
            },
            "actionable": bool(led_sig and md_ld is not None and md_ld >= 0.1),
            "actionable_note": (
                "actionable REQUIRES the lead-matched test (design B). It is "
                "significant here only if n_pairs>0 and |delta|>2*se and delta>=0.1 F."),
        }
    return out


def write_report(out):
    ART.mkdir(parents=True, exist_ok=True)
    js = ART / "nwp_cycle_convergence_2026-10-13.json"
    js.write_text(json.dumps(out, indent=2))

    L = []
    A = L.append
    A("# NWP cycle convergence — early (06Z-12Z) vs late (18Z-00Z) cycles")
    A("")
    A(f"Window: {out['window'][0]} → {out['window'][1]}. Read-only; truth = settlement_truth HIGH (trustworthy=1).")
    A(f"Early hours {out['cycle_phase_def']['early_hours']}, late hours {out['cycle_phase_def']['late_hours']}.")
    A("")
    A("**Design caveat:** " + out["design_note"])
    A("")
    A("| model | stn-days | vintages (E/L) | MAE early | MAE late | ΔE−L pooled |")
    A("|---|---:|---:|---:|---:|---:|")
    for m, r in out["models"].items():
        d = None
        if r["mae_early_pooled_all_vintages"] is not None and r["mae_late_pooled_all_vintages"] is not None:
            d = round(r["mae_early_pooled_all_vintages"] - r["mae_late_pooled_all_vintages"], 4)
        A(f"| {m} | {r['station_days']} | {r['n_vintage_early']}/{r['n_vintage_late']} | "
          f"{r['mae_early_pooled_all_vintages']} | {r['mae_late_pooled_all_vintages']} | {d} |")
    A("")
    A("## Paired tests (Δ = early_err − late_err; positive ⇒ late closer)")
    A("")
    A("| model | design | n | Δ (F) | s.e. | t | p | sig 2s.e. | late ≥0.1F |")
    A("|---|---|---:|---:|---:|---:|---:|:---:|:---:|")
    for m, r in out["models"].items():
        for key, dname in (("literal_paired", "literal"), ("lead_matched_paired", "lead-matched")):
            p = r[key]
            n = p.get("n_station_days_both", p.get("n_pairs"))
            A(f"| {m} | {dname} | {n} | {p['delta_early_minus_late_F']} | {p['se']} | "
              f"{p['t']} | {p['p_two_sided']} | {p['significant_2se']} | {p['late_better_by_ge_0.1F']} |")
    A("")
    A("## MAE by phase × lead (days)")
    A("")
    for m, r in out["models"].items():
        A(f"### {m}")
        A("| lead | early MAE | late MAE |")
        A("|---:|---:|---:|")
        e = r["mae_by_phase_by_lead"].get("early", {})
        l = r["mae_by_phase_by_lead"].get("late", {})
        for lead in sorted({int(x) for x in list(e) + list(l)}):
            A(f"| {lead} | {e.get(str(lead))} | {l.get(str(lead))} |")
        A("")
    A("## Verdict")
    A("")
    for m, r in out["models"].items():
        lm = r["lead_matched_paired"]
        lp = r["literal_paired"]
        A(f"- **{m}**: lead-matched pairs n={lm['n_pairs']} → actionable={r['actionable']}. "
          f"Literal Δ={lp['delta_early_minus_late_F']} F (n={lp['n_station_days_both']}, "
          f"sig@2s.e.={lp['significant_2se']}).")
    A("")
    A("Because the archive fetches ~once per model per day, cycle phase and lead time are "
      "confounded and no lead-matched early-vs-late comparison exists. The literal Δ "
      "therefore measures the lead-time gradient, not cycle-age skill: do NOT weight by "
      "cycle-age on this evidence. `actionable` is true only when the lead-matched test "
      "has pairs and clears both the 2 s.e. and the 0.1 F bars.")
    (ART / "nwp_cycle_convergence_2026-10-13.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    truth = load_truth()
    vin = load_all()
    out = analyze(truth, vin)
    write_report(out)
    for m, r in out["models"].items():
        print(f"{m:9} stn-days={r['station_days']:5} MAE E={r['mae_early_pooled_all_vintages']} "
              f"L={r['mae_late_pooled_all_vintages']} "
              f"literal Δ={r['literal_paired']['delta_early_minus_late_F']} (n={r['literal_paired']['n_station_days_both']}) "
              f"lead-matched Δ={r['lead_matched_paired']['delta_early_minus_late_F']} (n={r['lead_matched_paired']['n_pairs']})")
    print(f"artifacts → {ART}")