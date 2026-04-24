# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "generate_bench_from_pack.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("generate_bench_cli", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["generate_bench_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cli_missing_slug_prints_help():
    cli = _load_cli()
    with pytest.raises(SystemExit) as err:
        cli.build_parser().parse_args([])
    assert err.value.code == 2  # argparse missing required arg


@pytest.mark.asyncio
async def test_cli_main_invokes_generate_from_pack(monkeypatch, tmp_path):
    cli = _load_cli()
    called = {}

    async def fake_gen(**kwargs):
        called.update(kwargs)
        return {
            "n_proposed": 2,
            "n_accepted": 1,
            "n_rejected": 1,
            "score": 0.7,
            "self_mrr": 0.8,
            "cascade_recall": 0.55,
        }

    monkeypatch.setattr(cli, "generate_from_pack", fake_gen)
    monkeypatch.setattr(
        cli,
        "AsyncAnthropic",
        lambda **kw: MagicMock(),
    )
    exit_code = await cli.main_async(
        [
            "--slug",
            "toy-board",
            "--output-dir",
            str(tmp_path),
            "--memory-root",
            str(tmp_path / "memory"),
        ]
    )
    assert exit_code == 0
    assert called["device_slug"] == "toy-board"
    assert called["escalate_rejects"] is False


@pytest.mark.asyncio
async def test_cli_dry_run_skips_writes(monkeypatch, tmp_path):
    cli = _load_cli()

    async def fake_gen(**kwargs):
        return {
            "n_proposed": 0,
            "n_accepted": 0,
            "n_rejected": 0,
            "score": 0.0,
            "self_mrr": 0.0,
            "cascade_recall": 0.0,
        }

    monkeypatch.setattr(cli, "generate_from_pack", fake_gen)
    monkeypatch.setattr(cli, "AsyncAnthropic", lambda **kw: MagicMock())
    exit_code = await cli.main_async(
        [
            "--slug",
            "toy-board",
            "--output-dir",
            str(tmp_path),
            "--memory-root",
            str(tmp_path / "memory"),
            "--dry-run",
        ]
    )
    # 0 accepted still returns exit 1 per spec
    assert exit_code == 1
