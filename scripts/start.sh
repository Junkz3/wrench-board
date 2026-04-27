#!/usr/bin/env bash
# Start uvicorn with auto-bootstrap of Managed Agents resources on first run.
#
# When DIAGNOSTIC_MODE is unset or set to "managed" (default) and
# managed_ids.json is absent, this prints a one-screen warning, waits 5
# seconds for the operator to Ctrl+C, then runs
# scripts/bootstrap_managed_agent.py to materialise the environment + the
# four tier-scoped agents on the operator's Anthropic account. The IDs are
# persisted locally in managed_ids.json (gitignored). On subsequent starts
# the file is present so we go straight to uvicorn.
#
# To skip Managed Agents entirely (e.g. when the beta is unavailable on the
# operator's account), invoke `make demo-fallback` instead — it sets
# DIAGNOSTIC_MODE=direct and never reads managed_ids.json.
set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${DIAGNOSTIC_MODE:-managed}"
IDS_FILE="managed_ids.json"
PORT="${PORT:-8000}"
PYTHON=".venv/bin/python"
UVICORN=".venv/bin/uvicorn"

if [ "$MODE" != "direct" ] && [ ! -f "$IDS_FILE" ]; then
    cat <<'EOF'

  Managed Agents bootstrap required
  ─────────────────────────────────
  No managed_ids.json found. About to create on your
  Anthropic account:

    - 1 environment
    - 4 agents: fast (Haiku 4.5), normal (Sonnet 4.6),
      deep (Opus 4.7), curator (Sonnet 4.6)

  Idle resources (no cost until used). The IDs are persisted
  locally in managed_ids.json (gitignored).

  To skip and use the direct (Messages API) fallback instead,
  Ctrl+C now and run: make demo-fallback

  Auto-bootstrapping in 5 seconds...

EOF
    sleep 5
    "$PYTHON" scripts/bootstrap_managed_agent.py
    echo
    echo "Bootstrap complete. Starting uvicorn..."
    echo
fi

exec "$UVICORN" api.main:app --reload --host 0.0.0.0 --port "$PORT"
