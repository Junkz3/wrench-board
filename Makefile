.PHONY: install run test test-all test-fast test-eval lint format clean help build-field-corpus demo-fallback pin-cdn tools-inventory doctor

PYTHON ?= python3
VENV ?= .venv
PIP = $(VENV)/bin/pip
PY = $(VENV)/bin/python
UVICORN = $(VENV)/bin/uvicorn
PYTEST = $(VENV)/bin/pytest
RUFF = $(VENV)/bin/ruff

PORT ?= 8000
# Bind interface for `make run` and `make demo-fallback`. Defaults to
# 127.0.0.1 so the dev server is local-only and not reachable from the LAN.
# Override for LAN access (e.g. testing from a phone): `make run HOST=0.0.0.0`.
HOST ?= 127.0.0.1

help:
	@echo "wrench-board — common tasks"
	@echo ""
	@echo "  make install   Create .venv and install dependencies (incl. dev)"
	@echo "  make run       Start uvicorn in dev mode on port $(PORT) with --reload"
	@echo "  make test      Run pytest (fast subset, skips slow benchmarks) — live output, --durations=10"
	@echo "  make test-all  Run all pytest tests (incl. slow accuracy benchmarks)"
	@echo "  make test-fast Run pytest with -x --ff (stop at first fail, failures-first next time)"
	@echo "  make lint      Run ruff check"
	@echo "  make format    Run ruff format"
	@echo "  make clean     Remove caches (keeps .venv)"
	@echo "  make tools-inventory  Regenerate docs/tools.md from api/agent/manifest.py"
	@echo "  make doctor    Run local health check (env/pack/board) — exit 1 on critical failure"

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

run:
	@PORT=$(PORT) HOST=$(HOST) bash scripts/start.sh

# Rebuild the field-calibrated benchmark fixture from persisted data
# (live outcome.json + legacy field_reports/*.md). Commit the fixture
# after running so diffs show corpus drift.
build-field-corpus:
	$(PY) scripts/build_benchmark_corpus.py

# `python -u -m pytest` (unbuffered) so progress streams live when the output
# is piped or redirected — `pytest` directly buffers output in those cases and
# you only see the result at the end. `--tb=short` keeps tracebacks compact;
# `--durations=10` flags the 10 slowest tests so we can mark them `@slow` if
# they shouldn't be in the fast subset.
test:
	$(PY) -u -m pytest tests/ -v --tb=short --durations=10 -m "not slow"

test-all:
	$(PY) -u -m pytest tests/ -v --tb=short --durations=10

# Iteration-friendly: stop at first failure, then re-run failures first next
# time. Use during active debugging; `make test` for the full sweep.
test-fast:
	$(PY) -u -m pytest tests/ -v --tb=short -x --ff -m "not slow"

# Score floor guard: fail the build if the simulator + hypothesize stack
# drops below 0.5 on the frozen MNT Reform bench. The floor only becomes
# meaningful once axes 2/3 are fully implemented — until then the gate is
# informational. Intentionally non-fatal on missing graph (exit 2 from the
# CLI bubbles up so the failure reason is visible).
test-eval:
	@SCORE=$$($(PY) -m scripts.eval_simulator --device mnt-reform-motherboard | $(PY) -c "import json, sys; print(json.loads(sys.stdin.read())['score'])"); \
		echo "simulator score = $$SCORE"; \
		$(PY) -c "import sys; sys.exit(0 if float('$$SCORE') >= 0.5 else 1)" || (echo "FAIL: score below 0.5 floor" && exit 1)

lint:
	$(RUFF) check api/ tests/

format:
	$(RUFF) format api/ tests/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +

# Demo plan B — restart uvicorn in direct (non-Managed-Agents) mode.
# Use if Managed Agents API has an outage during the demo.
demo-fallback:
	@echo "Switching to direct (non-MA) diagnostic mode and restarting uvicorn"
	DIAGNOSTIC_MODE=direct $(UVICORN) api.main:app --host $(HOST) --port $(PORT)

# Mirror the CDN dependencies into web/vendor/ for offline-resilient demo.
# Vendored files are gitignored (re-fetched on demand).
pin-cdn:
	bash scripts/pin_cdn.sh

# Local health check — surfaces missing .env / managed_ids / packs / parsers
# in ~1s with a colored report. Exit 1 if any CRITICAL check fails.
doctor:
	@$(PY) scripts/doctor.py

# Re-generate docs/tools.md from api/agent/manifest.py. Idempotent: the
# script writes only when the rendered body actually changed. Run after
# adding / editing / removing any tool in MB_TOOLS, BV_TOOLS, etc., and
# commit docs/tools.md alongside the manifest change.
tools-inventory:
	$(PY) scripts/dump_tools_inventory.py

# --- Evolve (overnight self-improvement loop) ---

.PHONY: evolve-bootstrap evolve-run evolve-run-bg evolve-stop evolve-status

evolve-bootstrap:
	@./scripts/evolve-bootstrap.sh

evolve-run:
	@./scripts/evolve-runner.sh

evolve-run-bg:
	@nohup ./scripts/evolve-runner.sh >> /tmp/microsolder-evolve.log 2>&1 &
	@echo "Evolve runner started in background. Tail: tail -f /tmp/microsolder-evolve.log"
	@echo "Stop:  make evolve-stop"

evolve-stop:
	@if [ -f /tmp/microsolder-evolve.lock ]; then \
		PID=$$(cat /tmp/microsolder-evolve.lock); \
		echo "Killing runner PID $$PID"; \
		kill $$PID 2>/dev/null || true; \
		rm -f /tmp/microsolder-evolve.lock; \
	fi
	@pkill -f '[e]volve-runner.sh' 2>/dev/null || true
	@echo "Evolve runner stopped."

evolve-status:
	@echo "=== State ==="
	@cat evolve/state.json 2>/dev/null || echo "(not initialized)"
	@echo ""
	@echo "=== Last 10 results ==="
	@tail -10 evolve/results.tsv 2>/dev/null || echo "(no results yet)"
	@echo ""
	@echo "=== Lock ==="
	@if [ -f /tmp/microsolder-evolve.lock ]; then echo "Locked by PID $$(cat /tmp/microsolder-evolve.lock)"; else echo "No lock"; fi
	@echo ""
	@echo "=== Last 20 log lines ==="
	@tail -20 /tmp/microsolder-evolve.log 2>/dev/null || echo "(no log)"
