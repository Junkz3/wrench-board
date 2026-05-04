"""Unit tests for the scenario-filter wiring of `scripts/eval_diagnostic_agent.py`.

All tests are fast (no `slow` marker), no network, no WS, no Anthropic API
call. The runner is loaded as a module via importlib so we can exercise:

  - `filter_scenarios()` — the pure filtering function
  - `build_parser()` — argparse wiring (mutual exclusion, type coercion)
  - run_bench() output payload — only the JSON-shape contract
    (`n_scenarios_total`), via a stubbed asyncio path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_diagnostic_agent.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_diagnostic_agent", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    # 3.12 dataclass needs the module in sys.modules during exec_module
    sys.modules["eval_diagnostic_agent"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


eval_diag = _load_module()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_scenarios() -> list[dict]:
    """Five fake scenarios mirroring the real bench's id-shape."""
    return [
        {"id": "mnt-reform-vin-dead", "device_slug": "mnt-reform"},
        {"id": "iphone-x-vddmain-short", "device_slug": "iphone-x"},
        {"id": "iphone-x-pmic-gpu-rail", "device_slug": "iphone-x"},
        {"id": "iphone-12-vddmain-short", "device_slug": "iphone-12"},
        {"id": "demo-pi-3v3-dead", "device_slug": "demo-pi"},
    ]


# ---------------------------------------------------------------------------
# filter_scenarios — pure function
# ---------------------------------------------------------------------------


def test_no_filter_returns_all():
    """No flags → identical list (order preserved)."""
    sc = _fake_scenarios()
    out = eval_diag.filter_scenarios(sc)
    assert [s["id"] for s in out] == [s["id"] for s in sc]


def test_scenario_id_single():
    sc = _fake_scenarios()
    out = eval_diag.filter_scenarios(sc, scenario_ids=["mnt-reform-vin-dead"])
    assert len(out) == 1
    assert out[0]["id"] == "mnt-reform-vin-dead"


def test_scenario_id_multi_preserves_source_order():
    """Selection preserves the bench file order, regardless of arg order.

    Justification: the bench file is the authoritative ordering (scout
    comes before iphone-12 in the JSONL); reproducing arg-order would
    break determinism for downstream diff/aggregator tooling. The CLI
    --help docstring matches this behavior.
    """
    sc = _fake_scenarios()
    out = eval_diag.filter_scenarios(
        sc,
        # arg order: 12 first, then mnt — but file order is mnt then 12
        scenario_ids=["iphone-12-vddmain-short", "mnt-reform-vin-dead"],
    )
    assert [s["id"] for s in out] == [
        "mnt-reform-vin-dead",
        "iphone-12-vddmain-short",
    ]


def test_scenario_id_unknown_raises():
    sc = _fake_scenarios()
    with pytest.raises(eval_diag.ScenarioFilterError) as excinfo:
        eval_diag.filter_scenarios(sc, scenario_ids=["does-not-exist"])
    assert "unknown scenario id" in str(excinfo.value)


def test_scenario_id_unknown_exits_with_code_2(monkeypatch, capsys):
    """End-to-end: main() must return 2 on unknown id (not raise)."""
    bench_path = Path(__file__).parent / "_tmp_bench.jsonl"
    bench_path.write_text(
        "\n".join(json.dumps(s) for s in _fake_scenarios()) + "\n"
    )
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(
            sys, "argv",
            [
                "eval_diagnostic_agent.py",
                "--bench", str(bench_path),
                "--scenario-id", "does-not-exist",
            ],
        )
        rc = eval_diag.main()
        assert rc == 2
        captured = capsys.readouterr()
        assert "unknown scenario id" in captured.err
    finally:
        bench_path.unlink(missing_ok=True)


def test_bench_subset():
    sc = _fake_scenarios()
    out = eval_diag.filter_scenarios(sc, bench_subset=2)
    assert [s["id"] for s in out] == [
        "mnt-reform-vin-dead",
        "iphone-x-vddmain-short",
    ]


def test_bench_subset_one():
    sc = _fake_scenarios()
    out = eval_diag.filter_scenarios(sc, bench_subset=1)
    assert len(out) == 1
    assert out[0]["id"] == "mnt-reform-vin-dead"


@pytest.mark.parametrize("bad_value", [0, -1, -5])
def test_bench_subset_zero_or_negative_rejected(bad_value):
    sc = _fake_scenarios()
    with pytest.raises(eval_diag.ScenarioFilterError):
        eval_diag.filter_scenarios(sc, bench_subset=bad_value)


def test_max_scenarios_caps_subset():
    sc = _fake_scenarios()
    out = eval_diag.filter_scenarios(
        sc,
        scenario_ids=[
            "mnt-reform-vin-dead",
            "iphone-x-vddmain-short",
            "iphone-x-pmic-gpu-rail",
            "iphone-12-vddmain-short",
        ],
        max_scenarios=2,
    )
    assert len(out) == 2
    # Source-order preserved: mnt-reform first, iphone-x-vddmain-short second
    assert [s["id"] for s in out] == [
        "mnt-reform-vin-dead",
        "iphone-x-vddmain-short",
    ]


@pytest.mark.parametrize("bad_value", [0, -1])
def test_max_scenarios_zero_or_negative_rejected(bad_value):
    sc = _fake_scenarios()
    with pytest.raises(eval_diag.ScenarioFilterError):
        eval_diag.filter_scenarios(sc, max_scenarios=bad_value)


def test_subset_and_id_mutually_exclusive_pure():
    """Pure-function level: passing both raises ScenarioFilterError."""
    sc = _fake_scenarios()
    with pytest.raises(eval_diag.ScenarioFilterError):
        eval_diag.filter_scenarios(
            sc,
            scenario_ids=["mnt-reform-vin-dead"],
            bench_subset=2,
        )


def test_subset_and_id_mutually_exclusive_argparse():
    """argparse level: SystemExit (code 2) on both flags."""
    parser = eval_diag.build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args([
            "--bench-subset", "1",
            "--scenario-id", "mnt-reform-vin-dead",
        ])
    assert excinfo.value.code == 2


def test_max_scenarios_combines_with_bench_subset():
    """--bench-subset 4 then --max-scenarios 2 → 2 scenarios."""
    sc = _fake_scenarios()
    out = eval_diag.filter_scenarios(sc, bench_subset=4, max_scenarios=2)
    assert len(out) == 2
    assert [s["id"] for s in out] == [
        "mnt-reform-vin-dead",
        "iphone-x-vddmain-short",
    ]


def test_filter_empty_input_raises():
    with pytest.raises(eval_diag.ScenarioFilterError):
        eval_diag.filter_scenarios([])


# ---------------------------------------------------------------------------
# build_parser() type coercion
# ---------------------------------------------------------------------------


def test_parser_scenario_id_repeatable():
    """--scenario-id appends — multiple flags accumulate into a list."""
    parser = eval_diag.build_parser()
    args = parser.parse_args(
        ["--scenario-id", "A", "--scenario-id", "B", "--scenario-id", "C"]
    )
    assert args.scenario_id == ["A", "B", "C"]


def test_parser_bench_subset_int():
    parser = eval_diag.build_parser()
    args = parser.parse_args(["--bench-subset", "3"])
    assert args.bench_subset == 3


def test_parser_max_scenarios_int():
    parser = eval_diag.build_parser()
    args = parser.parse_args(["--max-scenarios", "2"])
    assert args.max_scenarios == 2


def test_parser_no_flags_defaults_none():
    parser = eval_diag.build_parser()
    args = parser.parse_args([])
    assert args.scenario_id is None
    assert args.bench_subset is None
    assert args.max_scenarios is None


# ---------------------------------------------------------------------------
# Output JSON contract — n_scenarios_total + n_scenarios coexist
# ---------------------------------------------------------------------------


def test_output_json_includes_n_scenarios_total(monkeypatch, tmp_path):
    """run_bench() output must include n_scenarios_total alongside n_scenarios.

    We stub _play_scenario to bypass WS/Anthropic and assert the payload shape.
    """
    bench_path = tmp_path / "fake_bench.jsonl"
    bench_path.write_text(
        "\n".join(json.dumps(s) for s in _fake_scenarios()) + "\n"
    )

    # Stub: return a benign ScenarioResult per scenario.
    async def _fake_play(host, tier, scenario, judge_client, verbose):
        return eval_diag.ScenarioResult(
            id=scenario["id"],
            device_slug=scenario["device_slug"],
            tier=tier,
            binary_score=1.0,
            judge_score=1.0,
            final_score=1.0,
            cost_usd=0.0,
            runtime_seconds=0.0,
        )

    # Stub Anthropic() so we don't need a key
    monkeypatch.setattr(eval_diag, "Anthropic", lambda: object())
    monkeypatch.setattr(eval_diag, "_play_scenario", _fake_play)

    args = SimpleNamespace(
        bench=str(bench_path),
        tier="normal",
        host="http://localhost:8000",
        verbose=False,
        scenario_id=["mnt-reform-vin-dead"],
        bench_subset=None,
        max_scenarios=None,
    )

    import asyncio
    payload = asyncio.run(eval_diag.run_bench(args))

    assert payload["n_scenarios"] == 1
    assert payload["n_scenarios_total"] == 5
    # Existing contract preserved
    assert "score" in payload
    assert payload["tier_under_test"] == "normal"


def test_output_json_n_total_equal_when_no_filter(monkeypatch, tmp_path):
    bench_path = tmp_path / "fake_bench.jsonl"
    bench_path.write_text(
        "\n".join(json.dumps(s) for s in _fake_scenarios()) + "\n"
    )

    async def _fake_play(host, tier, scenario, judge_client, verbose):
        return eval_diag.ScenarioResult(
            id=scenario["id"],
            device_slug=scenario["device_slug"],
            tier=tier,
            binary_score=1.0,
            judge_score=1.0,
            final_score=1.0,
        )

    monkeypatch.setattr(eval_diag, "Anthropic", lambda: object())
    monkeypatch.setattr(eval_diag, "_play_scenario", _fake_play)

    args = SimpleNamespace(
        bench=str(bench_path),
        tier="normal",
        host="http://localhost:8000",
        verbose=False,
        scenario_id=None,
        bench_subset=None,
        max_scenarios=None,
    )

    import asyncio
    payload = asyncio.run(eval_diag.run_bench(args))

    assert payload["n_scenarios"] == 5
    assert payload["n_scenarios_total"] == 5
