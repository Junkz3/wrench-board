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


def _mock_vision_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, count: int
) -> None:
    """Drive the synchronous (pipelined) ingest path without a real PDF.

    Stubs the boundaries the pipeline crosses: probe_page_count → count,
    ensure_renderable_pdf → identity (no ghostscript on the tiny fake PDF),
    _prepare_page → a fake RenderedPage per page, extract_page → a fake graph.
    """
    fake_by_number = {
        rp.page_number: rp
        for rp in _fake_rendered_pages(tmp_path / "render", count)
    }
    monkeypatch.setattr(orchestrator, "ensure_renderable_pdf", lambda p, _d: p)
    monkeypatch.setattr(orchestrator, "probe_page_count", lambda _p: count)

    def _fake_prepare(
        pdf_path, page_number, total_pages, pages_dir, render_dpi, use_grounding
    ):
        return fake_by_number[page_number], None

    monkeypatch.setattr(orchestrator, "_prepare_page", _fake_prepare)

    async def _fake_extract_page(*, rendered, **_):
        return _fake_page_graph(rendered.page_number)

    monkeypatch.setattr(orchestrator, "extract_page", _fake_extract_page)


@pytest.mark.asyncio
async def test_orchestrator_writes_artefacts_and_returns_electrical_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _mock_vision_pipeline(monkeypatch, tmp_path, 3)

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
async def test_ingest_emits_phase_step_per_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """ingest_schematic emits a live `phase_step page` as each page is processed.

    The landing UI renders "page 3/12" into the schematic-ingest line. Pages
    fan out in parallel, so the event carries a running done-count + total.
    """
    _mock_vision_pipeline(monkeypatch, tmp_path, 3)

    steps: list[dict] = []

    async def collect(ev: dict) -> None:
        steps.append(ev)

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.0\n")

    await orchestrator.ingest_schematic(
        device_slug="demo-device",
        pdf_path=fake_pdf,
        client=object(),
        memory_root=tmp_path / "memory",
        model="claude-opus-4-8",
        use_grounding=False,
        cache_warmup_seconds=0.0,
        on_event=collect,
    )

    page_steps = [
        e for e in steps
        if e.get("type") == "phase_step" and e.get("step") == "page"
    ]
    assert len(page_steps) == 3
    assert all(e["phase"] == "schematic_ingest" for e in page_steps)
    assert all(e["total"] == 3 for e in page_steps)
    # Done-counter ticks 1,2,3 regardless of which page finished first.
    assert sorted(e["index"] for e in page_steps) == [1, 2, 3]


@pytest.mark.asyncio
async def test_ingest_runs_without_on_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """on_event is optional — omitting it must not crash ingestion."""
    _mock_vision_pipeline(monkeypatch, tmp_path, 2)

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.0\n")

    result = await orchestrator.ingest_schematic(
        device_slug="demo-device",
        pdf_path=fake_pdf,
        client=object(),
        memory_root=tmp_path / "memory",
        model="claude-opus-4-8",
        use_grounding=False,
        cache_warmup_seconds=0.0,
    )
    assert result.device_slug == "demo-device"


@pytest.mark.asyncio
async def test_orchestrator_handles_single_page_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Single-page PDFs must skip the warmup gather and still produce valid
    artefacts."""
    _mock_vision_pipeline(monkeypatch, tmp_path, 1)

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


def _enable_batch_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip PIPELINE_VISION_BATCH on and force get_settings() to re-read."""
    import api.config as config_mod

    monkeypatch.setenv("PIPELINE_VISION_BATCH", "true")
    monkeypatch.setattr(config_mod, "_settings", None)


@pytest.mark.asyncio
async def test_batch_mode_caches_batch_results_and_direct_fallbacks_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Flag on: uncached pages go through the batch pass; its successes are
    written to the per-page cache (so the direct gather loads them from disk),
    and pages the batch could NOT produce fall back to the direct path."""
    _enable_batch_mode(monkeypatch)

    from api.pipeline.schematic import batch_vision

    fake_rendered = _fake_rendered_pages(tmp_path / "render", 3)
    monkeypatch.setattr(orchestrator, "render_pages", lambda *_, **__: fake_rendered)

    memory_root = tmp_path / "memory"
    pages_dir = memory_root / "batch-device" / "schematic_pages"
    pages_dir.mkdir(parents=True)
    # Page 1 is pre-cached on disk → must NOT be sent to the batch.
    (pages_dir / "page_001.json").write_text(_fake_page_graph(1).model_dump_json())

    batch_seen: dict = {}

    async def _fake_batch(*, pages, groundings, **kwargs):
        batch_seen["pages"] = [p.page_number for p in pages]
        batch_seen["groundings"] = groundings
        # Page 3 fails inside the batch → absent from the mapping.
        return {2: _fake_page_graph(2)}

    monkeypatch.setattr(batch_vision, "extract_pages_batch", _fake_batch)

    direct_pages: list[int] = []

    async def _fake_extract_page(*, rendered, **_):
        direct_pages.append(rendered.page_number)
        return _fake_page_graph(rendered.page_number)

    monkeypatch.setattr(orchestrator, "extract_page", _fake_extract_page)

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.0\n")

    result = await orchestrator.ingest_schematic(
        device_slug="batch-device",
        pdf_path=fake_pdf,
        client=object(),
        memory_root=memory_root,
        model="claude-opus-4-8",
        use_grounding=False,
        cache_warmup_seconds=0.0,
    )

    assert batch_seen["pages"] == [2, 3]  # uncached only
    assert direct_pages == [3]  # only the batch failure hits the direct path
    assert result.device_slug == "batch-device"
    for n in (1, 2, 3):
        assert (pages_dir / f"page_{n:03d}.json").is_file()


@pytest.mark.asyncio
async def test_batch_mode_off_never_touches_batch_vision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Default (flag off): the batch module must never be invoked."""
    from api.pipeline.schematic import batch_vision

    async def _boom(**_):
        raise AssertionError("extract_pages_batch must not be called when flag is off")

    monkeypatch.setattr(batch_vision, "extract_pages_batch", _boom)

    _mock_vision_pipeline(monkeypatch, tmp_path, 2)

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.0\n")

    result = await orchestrator.ingest_schematic(
        device_slug="direct-device",
        pdf_path=fake_pdf,
        client=object(),
        memory_root=tmp_path / "memory",
        use_grounding=False,
        cache_warmup_seconds=0.0,
    )
    assert len(result.components) == 2


def _fake_prepare_page_factory(events: list[tuple[int, str]], render_dir: Path):
    """Sync `_prepare_page` stand-in: records CPU spans, returns a fake page.

    Runs on a worker thread (the orchestrator calls it via asyncio.to_thread),
    so it uses a blocking sleep to occupy CPU time deterministically.
    """
    import time

    def _prepare(pdf_path, page_number, total_pages, output_dir, dpi, use_grounding):
        events.append((page_number, "cpu_start"))
        time.sleep(0.005)
        png = render_dir / f"page-{page_number:03d}.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"\x89PNG")
        rendered = RenderedPage(
            page_number=page_number,
            png_path=png,
            orientation="portrait",
            is_scanned=False,
            width_pt=595.0,
            height_pt=842.0,
        )
        events.append((page_number, "cpu_finish"))
        return rendered, None

    return _prepare


@pytest.mark.asyncio
async def test_vision_pipeline_overlaps_cpu_bounds_concurrency_and_warms_page_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Per-page render/ground (CPU) overlaps the vision wait, vision concurrency
    is capped, and page 1's vision completes before the rest start (warmup).

    The orchestrator pipelines each page through prepare (render + grounding,
    on a thread) then vision, so the CPU work hides under the OTPM-bound vision
    wait instead of running as a barrier before it. A semaphore caps vision
    concurrency; page 1 lands first to warm the shared-prefix cache.
    """
    import asyncio

    total = 6
    monkeypatch.setattr(orchestrator, "ensure_renderable_pdf", lambda p, _d: p)
    monkeypatch.setattr(orchestrator, "probe_page_count", lambda _p: total)

    events: list[tuple[int, str]] = []
    monkeypatch.setattr(
        orchestrator,
        "_prepare_page",
        _fake_prepare_page_factory(events, tmp_path / "render"),
    )

    vis_in_flight = 0
    max_vis_in_flight = 0

    async def _tracking_extract_page(*, rendered, **_):
        nonlocal vis_in_flight, max_vis_in_flight
        events.append((rendered.page_number, "vis_start"))
        vis_in_flight += 1
        max_vis_in_flight = max(max_vis_in_flight, vis_in_flight)
        await asyncio.sleep(0.02)  # vision is the long pole; CPU overlaps it
        vis_in_flight -= 1
        events.append((rendered.page_number, "vis_finish"))
        return _fake_page_graph(rendered.page_number)

    monkeypatch.setattr(orchestrator, "extract_page", _tracking_extract_page)

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.0\n")

    await orchestrator.ingest_schematic(
        device_slug="pipeline-device",
        pdf_path=fake_pdf,
        client=object(),
        memory_root=tmp_path / "memory",
        use_grounding=False,
        cache_warmup_seconds=0.0,
        vision_concurrency=3,
    )

    def _idx(target: tuple[int, str]) -> int:
        return events.index(target)

    # Concurrency cap respected and actually reached.
    assert max_vis_in_flight == 3, (max_vis_in_flight, events)

    # Warmup: page 1's vision finishes before any other page's vision starts.
    page1_vis_finish = _idx((1, "vis_finish"))
    other_vis_starts = [
        i for i, (p, kind) in enumerate(events) if kind == "vis_start" and p != 1
    ]
    assert other_vis_starts, "expected vision on pages beyond page 1"
    assert all(i > page1_vis_finish for i in other_vis_starts), events

    # Overlap: a later page's CPU prep runs while page 1's vision is in flight
    # (its CPU starts before page 1's vision finishes) — the whole point of the
    # pipeline. A pure barrier would do all CPU before any vision.
    assert _idx((2, "cpu_start")) < page1_vis_finish, events
