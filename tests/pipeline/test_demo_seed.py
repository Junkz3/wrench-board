# tests/pipeline/test_demo_seed.py
from pathlib import Path

from api.pipeline.demo_seed import seed_demo_packs


def _make_fixture(fixtures_root: Path) -> None:
    slug = fixtures_root / "demo-packs" / "mnt-reform-motherboard"
    (slug / "repairs").mkdir(parents=True)
    (slug / "electrical_graph.json").write_text('{"v": 1}')
    (slug / "repairs" / "example-mnt-reform.json").write_text('{"repair_id": "example-mnt-reform"}')


def test_seed_copies_when_absent(tmp_path):
    fixtures = tmp_path / "fixtures"
    _make_fixture(fixtures)
    memory = tmp_path / "memory"
    memory.mkdir()

    n = seed_demo_packs(memory, fixtures_root=fixtures)

    assert n == 1
    assert (memory / "mnt-reform-motherboard" / "electrical_graph.json").is_file()
    assert (memory / "mnt-reform-motherboard" / "repairs" / "example-mnt-reform.json").is_file()


def test_seed_is_idempotent_and_nondestructive(tmp_path):
    fixtures = tmp_path / "fixtures"
    _make_fixture(fixtures)
    memory = tmp_path / "memory"
    # Pre-existing pack with local edits MUST NOT be clobbered.
    existing = memory / "mnt-reform-motherboard"
    existing.mkdir(parents=True)
    (existing / "local-edit.json").write_text('{"keep": true}')

    n = seed_demo_packs(memory, fixtures_root=fixtures)

    assert n == 0  # already present → skipped
    assert (existing / "local-edit.json").is_file()  # untouched
