#!/usr/bin/env python3
"""Phase B Calibration Pipeline — one-shot efficient version."""
import sys, os, time, json, sqlite3
from collections import defaultdict
import numpy as np
from sklearn.isotonic import IsotonicRegression

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from core.unified_backtest import BACKTEST_SIGNALS, DB_PATH, load_station_data, compute_sharpe, compute_ece
from core.signals import SignalRegistry

STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS','KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX','KSAT','KSEA','KSFO']
SIGNALS = list(BACKTEST_SIGNALS)
TRAIN = 365; TEST = 90
SKIP = {'temperature_advection','intraday_metar_confirmation','ecmwf_bias_corrected','esdr','nwp_direct'}
FOR_STATION = {'fogr_reversion','metar_dtdt','pressure_tendency','nwp_dtdt_fusion'}
CONTEXT = {'nwp_direct','ai_composite'}
# For these signals, evaluate() returning None means 'no signal', DON'T fallback to for_station
STANDARD = {'gaussian','gaussian_v2','goldilocks','persistence','pressure_delta','calendar_climatology','wind_direction_shift','forecast_disagreement','frontal_detector','spread_based_entry','volume_momentum','settlement_arbitrage','seasonal_regime'}

conn = sqlite3.connect(DB_PATH)
reg = SignalRegistry(DB_PATH)
sigs = {n: reg.get_signal(n) for n in SIGNALS if n not in SKIP and reg.get_signal(n) is not None}

print(f"[{time.strftime('%H:%M:%S')}] Active: {len(sigs)} signals, 20 stations")
sys.stdout.flush()

t_start = time.time()
history = defaultdict(list)
results = defaultdict(lambda: defaultdict(list))
tdays = set()

for st_idx, station in enumerate(STATIONS):
    days, market = load_station_data(station, conn)
    if len(days) < TRAIN + TEST:
        print(f"  [{st_idx+1}] {station}: skip ({len(days)}d)"); sys.stdout.flush(); continue

    start = TRAIN
    while start + TEST <= len(days):
        for idx in range(start, min(start + TEST, len(days))):
            date = days[idx]['date']
            act = market.get(date)
            if act is None or act['settlement_bucket'] is None: continue
            pb = act.get('prev_bucket')
            if pb is None: continue
            adir = 'up' if act['settlement_bucket'] > pb else 'down'
            tdays.add((station, date))
            for nm, sig in sigs.items():
                d, c = None, 0.0
                if nm in FOR_STATION:
                    try: d, c = sig.evaluate_for_station(station, date, conn=conn)
                    except: pass
                else:
                    if nm in CONTEXT: sig._station = station
                    try: d, c = sig.evaluate(idx, days)
                    except: pass
                    if d is None and nm not in STANDARD and hasattr(sig, 'evaluate_for_station'):
                        try: d, c = sig.evaluate_for_station(station, date, conn=conn)
                        except: pass
                if d in ('up','down') and c >= 0:
                    ok = (d == adir)
                    history[(nm, station)].append((c, ok))
                    results[nm][station].append((d, adir, c, ok))
        start += TEST
    print(f"  [{st_idx+1}] {station}: {time.time()-t_start:.1f}s"); sys.stdout.flush()

conn.close()
print(f"Collection: {time.time()-t_start:.1f}s, {len(tdays)} station-days"); sys.stdout.flush()

# Fitting
calibs = {}; fallbacks = {}; global_cal = None; cell_cnt = {}
for sig in SIGNALS:
    for st in STATIONS:
        data = history.get((sig, st), [])
        if len(data) >= 200:
            X = np.array([d[0] for d in data], dtype=float)
            y = np.array([float(d[1]) for d in data], dtype=float)
            if len(np.unique(X)) >= 2 and len(np.unique(y)) >= 2:
                iso = IsotonicRegression(out_of_bounds='clip', y_min=0.05, y_max=0.95)
                iso.fit(X, y); calibs[(sig, st)] = iso; cell_cnt[sig] = cell_cnt.get(sig,0)+1

for sig in SIGNALS:
    data = [d for st in STATIONS for d in history.get((sig, st), [])]
    if len(data) >= 400:
        X = np.array([d[0] for d in data], dtype=float)
        y = np.array([float(d[1]) for d in data], dtype=float)
        if len(np.unique(X)) >= 2 and len(np.unique(y)) >= 2:
            iso = IsotonicRegression(out_of_bounds='clip', y_min=0.05, y_max=0.95)
            iso.fit(X, y); fallbacks[sig] = iso

all_data = [d for v in history.values() for d in v]
if len(all_data) >= 800:
    X = np.array([d[0] for d in all_data], dtype=float)
    y = np.array([float(d[1]) for d in all_data], dtype=float)
    if len(np.unique(X)) >= 2 and len(np.unique(y)) >= 2:
        iso = IsotonicRegression(out_of_bounds='clip', y_min=0.05, y_max=0.95)
        iso.fit(X, y); global_cal = iso

print(f"Calibrators: L1={len(calibs)}, L2={len(fallbacks)}, L3={'yes' if global_cal else 'no'}"); sys.stdout.flush()

def cal_conf(sig, st, rc):
    k = (sig, st)
    if k in calibs: return float(calibs[k].transform([np.clip(rc,0,1)])[0])
    if sig in fallbacks: return float(fallbacks[sig].transform([np.clip(rc,0,1)])[0])
    if global_cal: return float(global_cal.transform([np.clip(rc,0,1)])[0])
    return rc

def fb_level(sig, st):
    if (sig,st) in calibs: return "per_cell"
    if sig in fallbacks: return "per_signal"
    if global_cal: return "cross_signal"
    return "identity"

# Metrics
per_sig = {}; per_ss = {}
for sig in SIGNALS:
    rd = results.get(sig, {})
    total = sum(len(v) for v in rd.values())
    if total == 0:
        per_sig[sig] = {"raw_accuracy":0,"calibrated_accuracy":0,"raw_brier":1,"calibrated_brier":1,"ece":1,"sharpe":0,"coverage":0,"total_trades":0,"calibration_cell_count":0,"fallback_level":"insufficient_data","status":"insufficient_data"}
        continue
    t_cor = 0; t_cal_cor = 0; t_tr = 0; rbs = 0.0; cbs = 0.0; cl = []
    for st in STATIONS:
        r = rd.get(st, [])
        if not r: continue
        s_cor = sum(1 for _,_,_,c in r if c); s_cal_cor = 0
        for p,a,rc,c in r:
            cc = cal_conf(sig, st, rc); up_r = rc if p=='up' else 1-rc; up_c = cc if p=='up' else 1-cc; out = 1 if a=='up' else 0
            rbs += (up_r-out)**2; cbs += (up_c-out)**2
            s_cal_cor += 1 if (p==a) else 0
        per_ss[f"{sig}.{st}"] = {"raw_accuracy":round(s_cor/len(r),4),"calibrated_accuracy":round(s_cal_cor/len(r),4),"raw_brier":round(rbs/len(r),4),"total_trades":len(r)}
        t_cor += s_cor; t_cal_cor += s_cal_cor; t_tr += len(r)
        cl.extend([(p,a,cal_conf(sig,st,c)) for p,a,c,_ in r])
    ra = t_cor/t_tr; ca = t_cal_cor/t_tr; rb = rbs/t_tr; cb = cbs/t_tr
    ece = compute_ece(cl); sh = compute_sharpe([(c,p==a) for p,a,c in cl])
    cov = t_tr/len(tdays) if tdays else 0
    bl = "identity"
    for s in STATIONS:
        l = fb_level(sig,s)
        if l=="per_cell": bl="per_cell"; break
        elif l=="per_signal" and bl!="per_cell": bl="per_signal"
        elif l=="cross_signal" and bl not in ("per_cell","per_signal"): bl="cross_signal"
    per_sig[sig] = {"raw_accuracy":round(ra,4),"calibrated_accuracy":round(ca,4),"raw_brier":round(rb,4),"calibrated_brier":round(cb,4),"ece":round(ece,4),"sharpe":round(sh,4),"coverage":round(cov,4),"total_trades":t_tr,"calibration_cell_count":cell_cnt.get(sig,0),"fallback_level":bl,"status":"calibrated"}
    print(f"  {sig:35s}: ra={ra:.4f} ca={ca:.4f} sh={sh:.2f} tr={t_tr} lv={bl}")

valid = [s for s,m in per_sig.items() if m['status']=='calibrated']
if valid:
    avg = lambda f: round(np.mean([per_sig[s][f] for s in valid]), 4)
    agg = {"avg_raw_accuracy":avg('raw_accuracy'),"avg_calibrated_accuracy":avg('calibrated_accuracy'),"avg_brier":avg('calibrated_brier'),"avg_ece":avg('ece'),"signals_above_60pct":sum(1 for s in valid if per_sig[s]['calibrated_accuracy']>0.60),"signals_above_65pct":sum(1 for s in valid if per_sig[s]['calibrated_accuracy']>0.65),"signals_with_valid_calibrator":sum(1 for s in valid if per_sig[s]['fallback_level'] in ('per_cell','per_signal')),"total_signals_processed":len(valid)}
else:
    agg = {"avg_raw_accuracy":0,"avg_calibrated_accuracy":0,"avg_brier":0,"avg_ece":0,"signals_above_60pct":0,"signals_above_65pct":0,"signals_with_valid_calibrator":0,"total_signals_processed":0}

out = {"metadata":{"timestamp":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"phase":"B","signals_calibrated":len(SIGNALS),"stations":len(STATIONS),"train_days":TRAIN,"test_days":TEST,"total_trading_days_processed":len(tdays)},"per_signal":per_sig,"per_signal_per_station":per_ss,"aggregate":agg}
os.makedirs(os.path.dirname('data/phaseB_calibration_results.json') or '.', exist_ok=True)
with open('data/phaseB_calibration_results.json','w') as f: json.dump(out, f, indent=2)
print(f"\nSaved to data/phaseB_calibration_results.json")
print(f"  calendar_climatology: {per_sig.get('calendar_climatology',{}).get('raw_accuracy',0):.4f} raw, {per_sig.get('calendar_climatology',{}).get('calibrated_accuracy',0):.4f} cal")
print(f"Done: {time.time()-t_start:.1f}s")