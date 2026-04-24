.PHONY: install run test lint format clean help build-field-corpus

PYTHON ?= python3
VENV ?= .venv
PIP = $(VENV)/bin/pip
PY = $(VENV)/bin/python
UVICORN = $(VENV)/bin/uvicorn
PYTEST = $(VENV)/bin/pytest
RUFF = $(VENV)/bin/ruff

PORT ?= 8000

help:
	@echo "microsolder-agent — common tasks"
	@echo ""
	@echo "  make install   Create .venv and install dependencies (incl. dev)"
	@echo "  make run       Start uvicorn in dev mode on port $(PORT) with --reload"
	@echo "  make test      Run pytest (fast subset, skips slow benchmarks)"
	@echo "  make test-all  Run all pytest tests (incl. slow accuracy benchmarks)"
	@echo "  make lint      Run ruff check"
	@echo "  make format    Run ruff format"
	@echo "  make clean     Remove caches (keeps .venv)"

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

run:
	$(UVICORN) api.main:app --reload --host 0.0.0.0 --port $(PORT)

# Rebuild the field-calibrated benchmark fixture from persisted data
# (live outcome.json + legacy field_reports/*.md). Commit the fixture
# after running so diffs show corpus drift.
build-field-corpus:
	$(PY) scripts/build_benchmark_corpus.py

test:
	$(PYTEST) tests/ -v -m "not slow"

test-all:
	$(PYTEST) tests/ -v

lint:
	$(RUFF) check api/ tests/

format:
	$(RUFF) format api/ tests/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
