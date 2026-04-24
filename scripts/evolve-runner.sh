#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Microsolder Evolve — Runner (infinite loop, agent-driven)
#
# Usage:
#   Foreground (smoke test):  ./scripts/evolve-runner.sh
#   Background (overnight):   nohup ./scripts/evolve-runner.sh 2>&1 &
#
# Override interval (default 60s between sessions):
#   EVOLVE_INTERVAL=120 ./scripts/evolve-runner.sh

set -uo pipefail
cd "$(dirname "$0")/.."

# Force C numeric locale — see evolve-bootstrap.sh for rationale.
export LC_NUMERIC=C

LOCKFILE="/tmp/microsolder-evolve.lock"
LOGFILE="/tmp/microsolder-evolve.log"
INTERVAL="${EVOLVE_INTERVAL:-60}"
SKILL_FILE=".claude/skills/microsolder-evolve/SKILL.md"

# --- Pre-flight ---
if [ ! -f "$SKILL_FILE" ]; then
  echo "ERROR: missing $SKILL_FILE — install the skill first." | tee -a "$LOGFILE"
  exit 1
fi

if [ ! -f "evolve/state.json" ] || [ ! -f "evolve/results.tsv" ]; then
  echo "ERROR: evolve/ not initialized. Run scripts/evolve-bootstrap.sh first." | tee -a "$LOGFILE"
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: 'claude' CLI not found in PATH." | tee -a "$LOGFILE"
  exit 1
fi

# --- Cleanup on exit ---
trap "rm -f $LOCKFILE; echo '[EVOLVE] runner stopped at $(date)' >> $LOGFILE; exit 0" EXIT INT TERM

echo "[EVOLVE] runner started at $(date)" >> "$LOGFILE"
echo "[EVOLVE] interval: ${INTERVAL}s, log: $LOGFILE" >> "$LOGFILE"

# --- Main loop ---
while true; do
  if [ -f "$LOCKFILE" ]; then
    # Another session is running (shouldn't happen since claude -p is synchronous,
    # but guards against accidental double-start).
    echo "[EVOLVE] lockfile present, skipping (PID in lock: $(cat $LOCKFILE))" >> "$LOGFILE"
    sleep "$INTERVAL"
    continue
  fi

  echo $$ > "$LOCKFILE"
  echo "" >> "$LOGFILE"
  echo "=== EVOLVE SESSION $(date) ===" >> "$LOGFILE"

  # Invoke a fresh Claude session with the skill as system prompt.
  # --max-turns 100: hard cap so a stuck session can't burn unlimited tokens.
  # --dangerously-skip-permissions: required for autonomous git/file ops.
  # Stream-json so the monitoring layer can see every thinking block,
  # tool call, and Opus message in real time. Text mode would only show
  # the final summary at session end.
  echo "Execute one evolve session." | claude -p \
    --dangerously-skip-permissions \
    --max-turns 100 \
    --system-prompt-file "$SKILL_FILE" \
    --output-format stream-json \
    --verbose \
    >> "$LOGFILE" 2>&1
  CLAUDE_EXIT=$?

  echo "=== EVOLVE EXIT $(date) (exit=$CLAUDE_EXIT) ===" >> "$LOGFILE"
  rm -f "$LOCKFILE"

  # --- Auto-review every N consecutive keeps ---
  # Counts keep lines since last review-checkpoint marker in results.tsv.
  # When threshold met, dispatches a fresh claude -p subagent that audits the
  # last N evolve commits, writes a markdown report under evolve/reviews/,
  # and appends a review-checkpoint row to results.tsv to reset the counter.
  REVIEW_THRESHOLD="${EVOLVE_REVIEW_EVERY:-3}"
  KEEPS_SINCE_REVIEW=$(awk -F'\t' '
    NR == 1 { next }
    $6 == "review-checkpoint" { c = 0; next }
    $6 == "keep" { c++ }
    END { print c+0 }
  ' evolve/results.tsv)

  if [ "$KEEPS_SINCE_REVIEW" -ge "$REVIEW_THRESHOLD" ]; then
    REVIEW_TS=$(date -u +%Y-%m-%d-%H%M)
    REVIEW_FILE="evolve/reviews/${REVIEW_TS}-auto.md"
    SHAS=$(awk -F'\t' '$6=="keep" {print $2}' evolve/results.tsv | tail -n "$REVIEW_THRESHOLD" | tr '\n' ' ')

    echo "" >> "$LOGFILE"
    echo "=== AUTO-REVIEW $(date) — last $REVIEW_THRESHOLD keeps: $SHAS ===" >> "$LOGFILE"

    REVIEW_PROMPT="You are auditing the last ${REVIEW_THRESHOLD} commits produced by the evolve agent on branch evolve/. SHAs to review: ${SHAS}. For EACH commit: read the diff (\`git show <sha> -- api/pipeline/schematic/\`), classify as one of (✅ Vrai fix | ⚠️ Convention défendable | ❌ Gaming / score-hack), justify with a 1-line physical-realism check (does the change reflect a real failure mode on a real MNT Reform component?), and recommend keep/annotate/revert. Watch especially for 'self-dead on silent' patterns where a component is marked dead just to break Jaccard tie clusters in evaluator.py without any cascade effect — that's gaming. Write your report to ${REVIEW_FILE} in French markdown with sections: Résumé exécutif (2-3 lignes), one section per commit, Recommandations globales. After writing, append exactly one line to evolve/results.tsv (TSV format, LC_NUMERIC=C printf '%s\\t-\\t0.000000\\t0.000000\\t0.000000\\treview-checkpoint\\t<one line summary>\\n'). Do NOT modify any code. Do NOT touch evolve/state.json. Exit when done."

    claude -p \
      --dangerously-skip-permissions \
      --max-turns 30 \
      --output-format stream-json \
      --verbose \
      "$REVIEW_PROMPT" >> "$LOGFILE" 2>&1
    REVIEW_EXIT=$?

    echo "=== AUTO-REVIEW EXIT $(date) (exit=$REVIEW_EXIT) ===" >> "$LOGFILE"
  fi

  sleep "$INTERVAL"
done
