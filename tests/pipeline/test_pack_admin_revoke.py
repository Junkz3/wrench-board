"""T8 — CLI pack-admin revoke (Option C)."""

import json
from datetime import UTC, datetime
from pathlib import Path

from api.cli.pack_admin import main as cli_main
from api.pipeline.pack_storage import (
    JournalEntry,
    append_journal,
    init_pack_layout,
    read_journal,
    write_promoted_facts,
)
from api.pipeline.schemas import Provenance, RegistryComponent

SLUG = "iphone-12"


def _make_prov(expansion_id="E-rev", owner="t1", status="promoted"):
    return Provenance(
        expansion_id=expansion_id, added_at=datetime.now(UTC),
        added_by_tenant=owner, confidence=0.5,
        source_kind="agent_expansion", sanitizer_actions=[], status=status,
    )


def _setup_promoted_expansion(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    comp = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[],
                             provenance=_make_prov())
    write_promoted_facts(memory_root, SLUG, file_name="registry.json", new_facts=[comp])
    append_journal(memory_root, SLUG, JournalEntry(
        id="E-rev", ts=datetime.now(UTC), owner_ref="t1", slug=SLUG,
        focus_symptoms=[], focus_refdes=[],
        delta_summary={"new_components": ["F-cmp-x"], "new_rules": []},
        scout_dump_range={"start": 0, "end": 0}, status="promoted",
    ))


def test_cli_revoke_removes_from_promoted(tmp_path):
    _setup_promoted_expansion(tmp_path)
    rc = cli_main(["revoke", "--memory-root", str(tmp_path), "--slug", SLUG,
                   "--expansion", "E-rev", "--reason", "halluciné"])
    assert rc == 0
    promo = json.loads((tmp_path / SLUG / "promoted" / "registry.json").read_text())
    assert not any(it["canonical_name"] == "U1300" for it in promo["items"])
    journal = list(read_journal(tmp_path, SLUG))
    assert next(e for e in journal if e.id == "E-rev").status == "revoked"


def test_cli_revoke_baseline_refused(tmp_path):
    init_pack_layout(tmp_path, SLUG)
    append_journal(tmp_path, SLUG, JournalEntry(
        id="baseline-pre-T8", ts=datetime.now(UTC), owner_ref=None, slug=SLUG,
        focus_symptoms=[], focus_refdes=[],
        delta_summary={}, scout_dump_range={"start": 0, "end": 0}, status="baseline",
    ))
    rc = cli_main(["revoke", "--memory-root", str(tmp_path), "--slug", SLUG,
                   "--expansion", "baseline-pre-T8"])
    assert rc != 0


def test_cli_revoke_unknown_expansion_nonzero(tmp_path):
    init_pack_layout(tmp_path, SLUG)
    rc = cli_main(["revoke", "--memory-root", str(tmp_path), "--slug", SLUG,
                   "--expansion", "E-nope"])
    assert rc != 0
