#!/bin/bash
# Wait for the config-budget proof, then summarize verdicts to a file.
while pgrep -f "config_budget_proof.py" >/dev/null 2>&1; do sleep 30; done
{
  echo "=== CONFIG-BUDGET PROOF COMPLETE $(date -Iseconds) ==="
  if [ -f data/config_budget_proof.json ]; then
    .venv-gefs/bin/python - <<'PY'
import json
d=json.load(open('data/config_budget_proof.json'))
for s,v in d.get('signals',{}).items():
    print(f"\n## {s}  (runtime {v.get('runtime_seconds')}s)")
    for b,t in v.get('per_budget',{}).items():
        print(f"  budget {b}: mean_top10={t.get('mean_top10_accuracy')} best={t.get('best_accuracy')} n_elig={t.get('n_eligible')} top1_idx={t.get('top1_idx')}")
    print("  VERDICT:", v.get('verdict'))
PY
  else
    echo "NO JSON OUTPUT"
  fi
} > data/logs/config_budget_PROOF_RESULT.txt 2>&1
