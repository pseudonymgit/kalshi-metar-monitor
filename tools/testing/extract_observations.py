#!/usr/bin/env python3
"""Extract historical observations from transition metadata for alert delivery testing."""
import os, sqlite3, json

conn = sqlite3.connect(os.environ["ALERT_DB_PATH"])

stations = {}
for r in conn.execute("""
    SELECT te.station, te.created_utc, te.transition_type,
           te.current_temp, te.metadata_json
    FROM transition_events te
    ORDER BY te.station, te.created_utc ASC
"""):
    station = r[0]
    created = r[1]
    ttype = r[2]
    temp = r[3]
    meta_json = r[4]

    if station not in stations:
        stations[station] = []

    meta = {}
    if meta_json:
        try:
            meta = json.loads(meta_json)
        except:
            pass

    obs_time = meta.get("obs_time", created)
    prev_temp = meta.get("prev_temp_f")

    stations[station].append({
        "obs_time": obs_time,
        "temp_f": temp,
        "prev_temp_f": prev_temp,
        "transition_type": ttype,
        "created_utc": created,
    })

output = {}
for station, events in stations.items():
    output[station] = {"total_events": len(events), "by_date": {}}
    for e in events:
        day = e["obs_time"][:10]
        if day not in output[station]["by_date"]:
            output[station]["by_date"][day] = []
        output[station]["by_date"][day].append(e)

OBS_FILE = os.environ.get("OBS_FILE", "/tmp/alerts-delivery-obs.json")
with open(OBS_FILE, "w") as f:
    json.dump(output, f, indent=2)

for station in sorted(output.keys()):
    days = sorted(output[station]["by_date"].keys())
    total = output[station]["total_events"]
    print(f"  {station}: {total} events across {len(days)} days ({days[0]} to {days[-1]})")

conn.close()
print(f"Observation sequences written to {OBS_FILE}")
