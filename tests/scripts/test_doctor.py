"""Unit tests for `scripts/doctor.py`.

All tests are fast (no `slow` marker), filesystem-only, no network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "doctor.py"


def _load_doctor():
    spec = importlib.util.spec_from_file_location("doctor", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["doctor"] = mod
    spec.loader.exec_module(mod)
    return mod


doctor = _load_doctor()


# ---------------------------------------------------------------------------
# env_file
# ---------------------------------------------------------------------------


def test_env_file_ok(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-fakekey-1234567890\n", encoding="utf-8")
    res = doctor.check_env_file(tmp_path)
    assert res.status == doctor.STATUS_OK
    assert "ANTHROPIC_API_KEY set" in res.message


def test_env_file_missing(tmp_path: Path):
    res = doctor.check_env_file(tmp_path)
    assert res.status == doctor.STATUS_FAIL
    assert "missing" in res.message.lower()


def test_env_file_empty_key(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=\nOTHER=foo\n", encoding="utf-8")
    res = doctor.check_env_file(tmp_path)
    assert res.status == doctor.STATUS_FAIL
    assert "empty" in res.message.lower()


def test_env_file_key_with_quotes(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text('ANTHROPIC_API_KEY="sk-ant-quoted-key"\n', encoding="utf-8")
    res = doctor.check_env_file(tmp_path)
    assert res.status == doctor.STATUS_OK


# ---------------------------------------------------------------------------
# managed_ids
# ---------------------------------------------------------------------------


def test_managed_ids_ok(tmp_path: Path):
    payload = {
        "environment_id": "env_abc",
        "agents": {
            "fast": {"id": "agent_fast"},
            "normal": {"id": "agent_normal"},
            "deep": {"id": "agent_deep"},
        },
    }
    (tmp_path / "managed_ids.json").write_text(json.dumps(payload), encoding="utf-8")
    res = doctor.check_managed_ids(tmp_path)
    assert res.status == doctor.STATUS_OK


def test_managed_ids_missing(tmp_path: Path):
    res = doctor.check_managed_ids(tmp_path)
    assert res.status == doctor.STATUS_WARN


def test_managed_ids_malformed_json(tmp_path: Path):
    (tmp_path / "managed_ids.json").write_text("{not valid json", encoding="utf-8")
    res = doctor.check_managed_ids(tmp_path)
    assert res.status == doctor.STATUS_FAIL


def test_managed_ids_missing_tier(tmp_path: Path):
    payload = {
        "environment_id": "env_abc",
        "agents": {"fast": {"id": "f"}, "normal": {"id": "n"}},
    }
    (tmp_path / "managed_ids.json").write_text(json.dumps(payload), encoding="utf-8")
    res = doctor.check_managed_ids(tmp_path)
    assert res.status == doctor.STATUS_FAIL
    assert "deep" in res.message


# ---------------------------------------------------------------------------
# memory_root + pack_health
# ---------------------------------------------------------------------------


def test_memory_root_empty(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    res = doctor.check_memory_root(mem)
    assert res.status == doctor.STATUS_WARN
    assert res.details["slugs"] == []


def test_memory_root_skips_underscore(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "_profile").mkdir()
    (mem / "iphone-x").mkdir()
    (mem / "mnt-reform").mkdir()
    res = doctor.check_memory_root(mem)
    assert res.status == doctor.STATUS_OK
    assert "_profile" not in res.details["slugs"]
    assert set(res.details["slugs"]) == {"iphone-x", "mnt-reform"}


def test_pack_health_full(tmp_path: Path):
    mem = tmp_path / "memory"
    pack = mem / "iphone-x"
    pack.mkdir(parents=True)
    for filename in (
        "registry.json",
        "knowledge_graph.json",
        "rules.json",
        "dictionary.json",
        "audit_verdict.json",
    ):
        (pack / filename).write_text("{}", encoding="utf-8")
    (pack / "simulator_reliability.json").write_text(
        json.dumps({"score": 0.42}), encoding="utf-8"
    )
    res = doctor.check_pack_health(mem, "iphone-x")
    assert res.status == doctor.STATUS_OK
    assert res.details["reliability_score"] == pytest.approx(0.42)
    assert "simulator_reliability.json" in res.details["schematic_present"]


def test_pack_health_partial(tmp_path: Path):
    mem = tmp_path / "memory"
    pack = mem / "partial"
    pack.mkdir(parents=True)
    (pack / "registry.json").write_text("{}", encoding="utf-8")
    res = doctor.check_pack_health(mem, "partial")
    # registry present (no FAIL) but other artefacts missing -> WARN
    assert res.status == doctor.STATUS_WARN
    assert "knowledge_graph.json" in res.details["missing"]


def test_pack_health_missing_registry(tmp_path: Path):
    mem = tmp_path / "memory"
    pack = mem / "broken"
    pack.mkdir(parents=True)
    (pack / "rules.json").write_text("{}", encoding="utf-8")
    res = doctor.check_pack_health(mem, "broken")
    assert res.status == doctor.STATUS_FAIL


# ---------------------------------------------------------------------------
# board_assets
# ---------------------------------------------------------------------------


def test_board_assets_unknown_extension(tmp_path: Path):
    repo = tmp_path
    assets = repo / "board_assets"
    assets.mkdir()
    # The check filters on .brd / .kicad_pcb / .pdf so a `.xyz` file is
    # never seen — instead, prove the WARN branch by giving an empty dir
    # of recognised files plus an unrelated foo.xyz that gets ignored.
    (assets / "ignored.xyz").write_text("not boardview", encoding="utf-8")
    res = doctor.check_board_assets(repo)
    assert res.status == doctor.STATUS_WARN
    assert "no .brd" in res.message


def test_board_assets_unsupported_extension(tmp_path: Path, monkeypatch):
    """When `parser_for` raises UnsupportedFormatError, the file lands in WARN."""
    from api.board.parser import base as parser_base

    repo = tmp_path
    assets = repo / "board_assets"
    assets.mkdir()
    f = assets / "nothing.brd"
    f.write_text("placeholder", encoding="utf-8")

    def fake_parser_for(path):
        raise parser_base.UnsupportedFormatError(f"no parser registered for {path.suffix}")

    monkeypatch.setattr(parser_base, "parser_for", fake_parser_for)

    res = doctor.check_board_assets(repo)
    assert res.status == doctor.STATUS_WARN
    probed_for_file = next(p for p in res.details["probed"] if p["file"] == "nothing.brd")
    assert probed_for_file["status"] == doctor.STATUS_WARN


def test_board_assets_parser_exception_marks_fail(tmp_path: Path, monkeypatch):
    """A surprise exception from `parser_for` is treated as FAIL."""
    from api.board.parser import base as parser_base

    repo = tmp_path
    assets = repo / "board_assets"
    assets.mkdir()
    f = assets / "broken.kicad_pcb"
    f.write_text("placeholder", encoding="utf-8")

    def fake_parser_for(path):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(parser_base, "parser_for", fake_parser_for)
    res = doctor.check_board_assets(repo)
    assert res.status == doctor.STATUS_FAIL


def test_board_assets_real_kicad(tmp_path: Path):
    """Smoke-test the real repo's board_assets to confirm dispatch works."""
    repo = doctor.REPO_ROOT
    res = doctor.check_board_assets(repo)
    # Repo install ships a real .kicad_pcb + .brd, dispatcher must succeed.
    assert res.status in (doctor.STATUS_OK, doctor.STATUS_WARN)
    assert any(p["file"].endswith(".kicad_pcb") for p in res.details["probed"])


# ---------------------------------------------------------------------------
# camera + python_env + disk_usage
# ---------------------------------------------------------------------------


def test_camera_returns_info():
    res = doctor.check_camera()
    assert res.status == doctor.STATUS_INFO
    assert "devices" in res.details


def test_python_env_no_venv(tmp_path: Path):
    res = doctor.check_python_env(tmp_path)
    assert res.status == doctor.STATUS_FAIL
    assert ".venv" in res.message


def test_python_env_real_repo():
    res = doctor.check_python_env(doctor.REPO_ROOT)
    # The real repo install must have anthropic + fastapi etc available.
    assert res.status == doctor.STATUS_OK


def test_disk_usage_returns_info(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "blob.bin").write_bytes(b"\x00" * 1024)
    res = doctor.check_disk_usage(tmp_path, mem)
    assert res.status == doctor.STATUS_INFO
    assert res.details["memory_bytes"] >= 1024


# ---------------------------------------------------------------------------
# Reporting + exit code + CLI
# ---------------------------------------------------------------------------


def test_format_report_plain():
    results = [
        doctor.CheckResult(doctor.STATUS_OK, "a", "looks good", {}),
        doctor.CheckResult(doctor.STATUS_WARN, "b", "kinda", {}),
    ]
    out = doctor.format_report(results, use_color=False)
    assert "[OK]" in out
    assert "[WARN]" in out
    assert "Summary" in out
    # No ANSI escape sequences in plain mode.
    assert "\033[" not in out


def test_format_report_color():
    results = [doctor.CheckResult(doctor.STATUS_FAIL, "x", "broken", {})]
    out = doctor.format_report(results, use_color=True)
    assert "\033[31m" in out  # red FAIL
    assert "\033[0m" in out


def test_exit_code_zero_when_no_fail():
    results = [
        doctor.CheckResult(doctor.STATUS_OK, "a", "ok", {}),
        doctor.CheckResult(doctor.STATUS_WARN, "b", "warn", {}),
    ]
    assert doctor.exit_code_for(results) == 0


def test_exit_code_one_when_any_fail():
    results = [
        doctor.CheckResult(doctor.STATUS_OK, "a", "ok", {}),
        doctor.CheckResult(doctor.STATUS_FAIL, "b", "broke", {}),
    ]
    assert doctor.exit_code_for(results) == 1


def test_results_to_json_parsable():
    results = [doctor.CheckResult(doctor.STATUS_OK, "x", "fine", {"k": 1})]
    payload = doctor.results_to_json(results)
    parsed = json.loads(payload)
    assert parsed["exit_code"] == 0
    assert parsed["results"][0]["name"] == "x"


def test_main_json_output(tmp_path: Path, capsys):
    # Point doctor at an empty memory root so most checks run cheaply.
    code = doctor.main(["--json", "--memory-root", str(tmp_path / "empty_memory")])
    captured = capsys.readouterr()
    assert isinstance(code, int)
    parsed = json.loads(captured.out)
    assert "results" in parsed
    assert "exit_code" in parsed


def test_main_text_no_color(tmp_path: Path, capsys):
    code = doctor.main(["--no-color", "--memory-root", str(tmp_path / "empty_memory")])
    captured = capsys.readouterr()
    assert isinstance(code, int)
    assert "Summary" in captured.out
    assert "\033[" not in captured.out
