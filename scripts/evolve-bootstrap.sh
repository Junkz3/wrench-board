#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Microsolder Evolve — Bootstrap (one-shot, run before the first night)
#
# Usage: scripts/evolve-bootstrap.sh
#
# Verifies all prerequisites (eval infra from spec axes 2/3, clean git tree),
# creates the evolve/<date> branch, measures baseline, writes evolve/state.json
# and evolve/results.tsv. Idempotent: safe to re-run.

set -euo pipefail
cd "$(dirname "$0")/.."

DATE=$(date +%Y-%m-%d)
EVOLVE_DIR="evolve"
RESULTS_TSV="$EVOLVE_DIR/results.tsv"
STATE_JSON="$EVOLVE_DIR/state.json"
BASELINE_LOG="/tmp/microsolder-evolve-baseline.json"

echo "==> Microsolder Evolve bootstrap ($DATE)"

# --- 1. Pre-requisite checks ---
echo "    Checking prerequisites..."

[ -d .git ] || { echo "ERROR: not a git repository"; exit 1; }

for f in scripts/eval_simulator.py api/pipeline/schematic/evaluator.py benchmark/scenarios.jsonl; do
  if [ ! -f "$f" ]; then
    echo "ERROR: missing prerequisite '$f'"
    echo "       This file must be implemented per the spec at"
    echo "       docs/superpowers/specs/2026-04-24-schematic-simulator-axes-2-3-design.md"
    exit 1
  fi
done

[ -d benchmark/sources ] || { echo "ERROR: missing benchmark/sources/ directory"; exit 1; }

SCENARIO_COUNT=$(wc -l < benchmark/scenarios.jsonl)
if [ "$SCENARIO_COUNT" -lt 10 ]; then
  echo "ERROR: benchmark/scenarios.jsonl has $SCENARIO_COUNT scenarios, need >= 10"
  exit 1
fi
echo "    Prerequisites OK ($SCENARIO_COUNT scenarios)"

# --- 2. Clean git tree check ---
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: tracked working tree dirty. Commit or stash before bootstrapping."
  exit 1
fi

# --- 3. Create or switch to evolve/<date> branch ---
CURRENT_BRANCH=$(git branch --show-current)
TARGET_BRANCH="evolve/$DATE"

if [[ "$CURRENT_BRANCH" =~ ^evolve/ ]]; then
  echo "    Already on evolve branch: $CURRENT_BRANCH"
  TARGET_BRANCH="$CURRENT_BRANCH"
else
  if git show-ref --verify --quiet "refs/heads/$TARGET_BRANCH"; then
    git checkout "$TARGET_BRANCH"
  else
    git checkout -b "$TARGET_BRANCH"
  fi
  echo "    Switched to branch: $TARGET_BRANCH"
fi

# --- 4. Init evolve/ ---
mkdir -p "$EVOLVE_DIR/reports"

if [ ! -f "$RESULTS_TSV" ]; then
  printf "timestamp\tcommit\tscore\tself_mrr\tcascade_recall\tstatus\tdescription\n" > "$RESULTS_TSV"
  echo "    Created $RESULTS_TSV (empty, header only)"
fi

# --- 5. Baseline measure ---
echo "    Measuring baseline (this may take a few minutes)..."
if ! timeout 600 python -m scripts.eval_simulator > "$BASELINE_LOG" 2>&1; then
  echo "ERROR: baseline eval_simulator failed. Output:"
  cat "$BASELINE_LOG"
  exit 1
fi

# --- 6. Validate JSON output and extract score ---
BASELINE_SCORE=$(python3 -c "
import json, sys
try:
    d = json.load(open('$BASELINE_LOG'))
    assert 'score' in d and 'self_mrr' in d and 'cascade_recall' in d, 'missing fields'
    print(d['score'])
except Exception as e:
    print(f'INVALID JSON: {e}', file=sys.stderr)
    sys.exit(1)
")

if [ -z "$BASELINE_SCORE" ]; then
  echo "ERROR: could not parse baseline score from $BASELINE_LOG"
  exit 1
fi

BASELINE_COMMIT=$(git rev-parse --short HEAD)

# --- 7. Write state.json ---
python3 -c "
import json
state = {
    'baseline_score': float('$BASELINE_SCORE'),
    'baseline_commit': '$BASELINE_COMMIT',
    'total_runs': 0,
    'last_run_at': None,
    'last_status': None,
    'last_5_statuses': [],
    'branch': '$TARGET_BRANCH',
}
with open('$STATE_JSON', 'w') as f:
    json.dump(state, f, indent=2)
print('    state.json written')
"

# --- 8. Append baseline as first entry in results.tsv ---
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SELF_MRR=$(python3 -c "import json; print(json.load(open('$BASELINE_LOG'))['self_mrr'])")
CASCADE=$(python3 -c "import json; print(json.load(open('$BASELINE_LOG'))['cascade_recall'])")
printf "%s\t%s\t%.6f\t%.6f\t%.6f\t%s\t%s\n" \
  "$TIMESTAMP" "$BASELINE_COMMIT" "$BASELINE_SCORE" "$SELF_MRR" "$CASCADE" \
  "baseline" "initial baseline measurement" >> "$RESULTS_TSV"

echo ""
echo "==> Bootstrap complete"
echo "    Branch:         $TARGET_BRANCH"
echo "    Baseline score: $BASELINE_SCORE (commit $BASELINE_COMMIT)"
echo "    Next:           ./scripts/evolve-runner.sh  (or: nohup ./scripts/evolve-runner.sh 2>&1 &)"
