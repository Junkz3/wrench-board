"""T8 — CLI pack-admin list-expansions / show-expansion / promote-stub (Option C)."""

from datetime import UTC, datetime
from pathlib import Path

from api.cli.pack_admin import main as cli_main
from api.pipeline.pack_storage import JournalEntry, append_journal, init_pack_layout

SLUG = "iphone-12"


def _seed_journal(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    append_journal(memory_root, SLUG, JournalEntry(
        id="E-aaa", ts=datetime.now(UTC), owner_ref="t1", slug=SLUG,
        focus_symptoms=["no sound"], focus_refdes=[],
        delta_summary={"new_components": ["F-cmp-1"], "new_rules": []},
        scout_dump_range={"start": 0, "end": 10}, status="promoted",
    ))


def test_cli_list_expansions(tmp_path, capsys):
    _seed_journal(tmp_path)
    rc = cli_main(["list-expansions", "--memory-root", str(tmp_path), "--slug", SLUG])
    assert rc == 0
    out = capsys.readouterr().out
    assert "E-aaa" in out
    assert "promoted" in out


def test_cli_list_expansions_filter_status(tmp_path, capsys):
    _seed_journal(tmp_path)
    rc = cli_main(["list-expansions", "--memory-root", str(tmp_path), "--slug", SLUG,
                   "--status", "revoked"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "E-aaa" not in out  # E-aaa is promoted, not revoked


def test_cli_show_expansion(tmp_path, capsys):
    _seed_journal(tmp_path)
    rc = cli_main(["show-expansion", "--memory-root", str(tmp_path), "--slug", SLUG,
                   "--expansion", "E-aaa"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "E-aaa" in out
    assert "no sound" in out


def test_cli_show_unknown_nonzero(tmp_path):
    _seed_journal(tmp_path)
    rc = cli_main(["show-expansion", "--memory-root", str(tmp_path), "--slug", SLUG,
                   "--expansion", "E-nope"])
    assert rc != 0


def test_cli_promote_is_v2_stub(tmp_path):
    """En Option C il n'y a pas de staging → promote n'est pas une opération V1.
    La commande existe (contrat) mais sort non-zéro en expliquant V2."""
    _seed_journal(tmp_path)
    rc = cli_main(["promote", "--memory-root", str(tmp_path), "--slug", SLUG,
                   "--expansion", "E-aaa"])
    assert rc != 0
