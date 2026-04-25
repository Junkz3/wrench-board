"""Unit tests for the pipeline phase narrator (offline, mocked)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.pipeline.phase_narrator import narrate_phase


def _make_anthropic_response(narration_text: str):
    """Build a fake Anthropic Messages response wrapping a tool_use block with `text`."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "narrate"
    block.input = {"text": narration_text}
    block.id = "toolu_test"
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "tool_use"
    resp.usage = MagicMock(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return resp


@pytest.mark.asyncio
async def test_narrate_scout_reads_raw_dump(tmp_path: Path):
    pack = tmp_path / "demo-device"
    pack.mkdir()
    (pack / "raw_research_dump.md").write_text(
        "# Demo Device\n\nThe device uses an STM32 MCU and a TPS65185 PMIC.\n"
        "Symptoms: dead screen, no boot.\n",
        encoding="utf-8",
    )

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_make_anthropic_response(
            "J'ai trouvé que ton appareil utilise un STM32 et un PMIC TPS65185. "
            "Les symptômes connus incluent un écran mort et un échec de boot."
        )
    )

    text = await narrate_phase(
        phase="scout", slug="demo-device", client=fake_client, memory_root=tmp_path
    )
    assert text.startswith("J'ai trouvé")
    assert len(text) <= 600  # cap enforced
    fake_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_narrate_registry_reads_registry_json(tmp_path: Path):
    pack = tmp_path / "demo-device"
    pack.mkdir()
    (pack / "registry.json").write_text(
        '{"components": [{"refdes": "U1"}, {"refdes": "U2"}], "signals": []}',
        encoding="utf-8",
    )

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_make_anthropic_response("J'ai catalogué 2 composants. Je peux maintenant construire le graphe.")
    )

    text = await narrate_phase(
        phase="registry", slug="demo-device", client=fake_client, memory_root=tmp_path
    )
    assert "catalogué" in text


@pytest.mark.asyncio
async def test_narrate_returns_empty_when_artifact_missing(tmp_path: Path):
    """If the artifact file doesn't exist on disk, narration is skipped silently."""
    pack = tmp_path / "demo-device"
    pack.mkdir()
    # No raw_research_dump.md written.

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock()  # should never be called

    text = await narrate_phase(
        phase="scout", slug="demo-device", client=fake_client, memory_root=tmp_path
    )
    assert text == ""
    fake_client.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_narrate_returns_empty_on_anthropic_error(tmp_path: Path):
    """Narrator failures must NEVER bubble up — return empty string instead."""
    pack = tmp_path / "demo-device"
    pack.mkdir()
    (pack / "raw_research_dump.md").write_text("anything", encoding="utf-8")

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=RuntimeError("anthropic down"))

    text = await narrate_phase(
        phase="scout", slug="demo-device", client=fake_client, memory_root=tmp_path
    )
    assert text == ""


@pytest.mark.asyncio
async def test_narrate_unknown_phase_returns_empty(tmp_path: Path):
    fake_client = MagicMock()
    text = await narrate_phase(
        phase="bogus_phase", slug="demo-device", client=fake_client, memory_root=tmp_path
    )
    assert text == ""
