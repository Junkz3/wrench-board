"""Integration test for the schematic orchestrator — fully mocked.

Replaces the renderer and `extract_page` boundary so no pdftoppm subprocess is
launched and no Anthropic API call is made. Verifies the orchestrator walks
the full render → grounding (off here) → vision → merge → compile → persist
chain and writes every expected artefact under `memory/{device_slug}/`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.pipeline.schematic import orchestrator
from api.pipeline.schematic.renderer import RenderedPage
from api.pipeline.schematic.schemas import (
    ComponentValue,
    PageNet,
    PageNode,
    PagePin,
    SchematicPageGraph,
    TypedEdge,
)


def _fake_rendered_pages(tmp_path: Path, count: int) -> list[RenderedPage]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(1, count + 1):
        png = tmp_path / f"page-{i:02d}.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")  # not a real PNG; never read here
        out.append(
            RenderedPage(
                page_number=i,
                png_path=png,
                orientation="portrait",
                is_scanned=False,
                width_pt=595.0,
                height_pt=842.0,
            )
        )
    return out


def _fake_page_graph(page: int) -> SchematicPageGraph:
    u7 = PageNode(
        refdes="U7",
        type="ic",
        value=ComponentValue(
            raw="LM2677SX-5",
            primary="LM2677SX-5",
            mpn="LM2677SX-5",
        ),
        page=page,
        pins=[
            PagePin(number="2", name="VIN", role="power_in", net_label="30V_GATE"),
            PagePin(number="7", name="ON/OFF", role="enable_in", net_label="5V_PWR_EN"),
        ],
    )
    c16 = PageNode(refdes="C16", type="capacitor", page=page)
    return SchematicPageGraph(
        page=page,
        sheet_name=f"Sheet {page}",
        sheet_path=f"/Sheet{page}/",
        nodes=[u7, c16],
        nets=[
            PageNet(
                local_id="n1",
                label="30V_GATE",
                is_power=True,
                is_global=True,
                connects=["U7.2", "C16.1"],
                page=page,
            ),
            PageNet(
                local_id="n2",
                label="+5V",
                is_power=True,
                is_global=True,
                connects=["U7.5"] if page == 1 else [],
                page=page,
            ),
        ],
        typed_edges=[
            TypedEdge(src="U7", dst="+5V", kind="powers", page=page),
            TypedEdge(src="U7", dst="30V_GATE", kind="powered_by", page=page),
            TypedEdge(src="5V_PWR_EN", dst="U7", kind="enables", page=page),
            TypedEdge(src="C16", dst="30V_GATE", kind="decouples", page=page),
        ],
    )


@pytest.mark.asyncio
async def test_orchestrator_writes_artefacts_and_returns_electrical_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_rendered = _fake_rendered_pages(tmp_path / "render", 3)

    monkeypatch.setattr(
        orchestrator, "render_pages", lambda *_, **__: fake_rendered
    )

    async def _fake_extract_page(*, rendered, **_):
        return _fake_page_graph(rendered.page_number)

    monkeypatch.setattr(orchestrator, "extract_page", _fake_extract_page)

    memory_root = tmp_path / "memory"
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.0\n")

    result = await orchestrator.ingest_schematic(
        device_slug="demo-device",
        pdf_path=fake_pdf,
        client=object(),  # unused because extract_page is mocked
        memory_root=memory_root,
        model="claude-opus-4-8",
        use_grounding=False,
        cache_warmup_seconds=0.0,
    )

    assert result.device_slug == "demo-device"
    assert "U7" in result.components
    assert "C16" in result.components
    # The 3 pages each contribute the same 2 nets → merged to 2 NetNodes.
    assert "30V_GATE" in result.nets
    assert "+5V" in result.nets
    # Electrical layer derives rails for nets flagged is_power.
    assert "30V_GATE" in result.power_rails
    assert "+5V" in result.power_rails
    # U7 powers +5V → should appear as a source_refdes on the +5V rail.
    assert result.power_rails["+5V"].source_refdes == "U7"
    # Boot sequence has at least one phase (U7 is root).
    assert len(result.boot_sequence) >= 1

    device_dir = memory_root / "demo-device"
    assert device_dir.is_dir()
    for n in (1, 2, 3):
        page_file = device_dir / "schematic_pages" / f"page_{n:03d}.json"
        assert page_file.is_file()
        data = json.loads(page_file.read_text())
        assert data["page"] == n
    assert (device_dir / "schematic_graph.json").is_file()
    assert (device_dir / "electrical_graph.json").is_file()


@pytest.mark.asyncio
async def test_orchestrator_handles_single_page_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Single-page PDFs must skip the gather() branch and still produce
    valid artefacts. No cache warmup sleep triggered."""
    fake_rendered = _fake_rendered_pages(tmp_path / "render", 1)
    monkeypatch.setattr(
        orchestrator, "render_pages", lambda *_, **__: fake_rendered
    )

    async def _fake_extract_page(*, rendered, **_):
        return _fake_page_graph(rendered.page_number)

    monkeypatch.setattr(orchestrator, "extract_page", _fake_extract_page)

    memory_root = tmp_path / "memory"
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.0\n")

    result = await orchestrator.ingest_schematic(
        device_slug="one-page",
        pdf_path=fake_pdf,
        client=object(),
        memory_root=memory_root,
        use_grounding=False,
        cache_warmup_seconds=0.0,
    )

    assert len(result.components) == 2  # U7, C16
    assert (memory_root / "one-page" / "schematic_pages" / "page_001.json").is_file()
    assert (memory_root / "one-page" / "electrical_graph.json").is_file()
