#!/usr/bin/env bash
# Run all regime sweeps in parallel batches (6 at a time).
set -u
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source
mkdir -p data/sweeps

# Arrays must be indexed in parallel: name[i], filter[i]
NAMES=()
FILTERS=()

add_regime() { NAMES+=("$1"); FILTERS+=("$2"); }

add_regime season_jja      /tmp/sweep_filter_jja.json
add_regime season_djf      /tmp/sweep_filter_djf.json
add_regime season_mam      /tmp/sweep_filter_mam.json
add_regime season_son      /tmp/sweep_filter_son.json
add_regime region_continental /tmp/sweep_filter_continental.json
add_regime region_marine   /tmp/sweep_filter_marine.json
add_regime region_arid     /tmp/sweep_filter_arid.json
add_regime region_subtropical /tmp/sweep_filter_subtropical.json
add_regime cycle_06z       /tmp/sweep_filter_06z.json
add_regime cycle_12z       /tmp/sweep_filter_12z.json
add_regime summer_continental /tmp/sweep_filter_summer_continental.json
add_regime summer_marine   /tmp/sweep_filter_summer_marine.json
add_regime winter_continental /tmp/sweep_filter_winter_continental.json
add_regime winter_marine   /tmp/sweep_filter_winter_marine.json
add_regime jja_06z         /tmp/sweep_filter_jja_06z.json
add_regime jja_12z         /tmp/sweep_filter_jja_12z.json
add_regime djf_06z         /tmp/sweep_filter_djf_06z.json
add_regime djf_12z         /tmp/sweep_filter_djf_12z.json
add_regime mam_06z         /tmp/sweep_filter_mam_06z.json
add_regime mam_12z         /tmp/sweep_filter_mam_12z.json
add_regime son_06z         /tmp/sweep_filter_son_06z.json
add_regime son_12z         /tmp/sweep_filter_son_12z.json
add_regime goldilocks      /tmp/sweep_filter_goldilocks.json
add_regime trajectory      /tmp/sweep_filter_trajectory.json

MAX_PARALLEL=6
active=0
for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"
  filter="${FILTERS[$i]}"
  python3 scripts/per_parameter_sweep.py --metric accuracy --fast \
    --filter-json "$filter" \
    --output-dir "data/sweeps/$name" \
    > "data/sweeps/${name}.log" 2>&1 &
  active=$((active+1))
  echo "Launched $name"
  if [ $active -ge $MAX_PARALLEL ]; then
    wait -n 2>/dev/null
    active=$((active-1))
  fi
done
wait
echo "ALL SWEEPS COMPLETE"