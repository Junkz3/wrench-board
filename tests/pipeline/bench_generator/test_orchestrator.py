# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api.pipeline.bench_generator.errors import (
    BenchGeneratorPreconditionError,
)
from api.pipeline.bench_generator.orchestrator import generate_from_pack


class _StubBlock:
    def __init__(self, name: str, payload: dict):
        self.type = "tool_use"
        self.name = name
        self.input = payload


class _StubResponse:
    def __init__(self, payload: dict):
        self.content = [_StubBlock("propose_scenarios", payload)]
        self.usage = MagicMock(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )


class _StubStream:
    def __init__(self, response: _StubResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self._response


def _write_graph(pack_dir: Path, toy_graph) -> None:
    (pack_dir / "electrical_graph.json").write_text(
        toy_graph.model_dump_json(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_missing_graph_raises_precondition(pack_dir, tmp_path):
    client = MagicMock()
    with pytest.raises(BenchGeneratorPreconditionError, match="electrical_graph"):
        await generate_from_pack(
            device_slug="toy-board",
            client=client,
            model="claude-sonnet-4-6",
            memory_root=pack_dir.parent,
            output_dir=tmp_path / "auto_proposals",
            latest_path=tmp_path / "auto_proposals" / "_latest.json",
            run_date="2026-04-24",
        )


@pytest.mark.asyncio
async def test_end_to_end_writes_six_files(pack_dir, toy_graph, sample_draft, tmp_path):
    _write_graph(pack_dir, toy_graph)
    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(
            _StubResponse(
                {
                    "scenarios": [sample_draft.model_dump()],
                }
            )
        )
    )
    out_dir = tmp_path / "auto_proposals"
    result = await generate_from_pack(
        device_slug="toy-board",
        client=client,
        model="claude-sonnet-4-6",
        memory_root=pack_dir.parent,
        output_dir=out_dir,
        latest_path=out_dir / "_latest.json",
        run_date="2026-04-24",
    )
    # Files on disk
    assert (out_dir / "toy-board-2026-04-24.jsonl").exists()
    assert (out_dir / "toy-board-2026-04-24.rejected.jsonl").exists()
    assert (out_dir / "toy-board-2026-04-24.manifest.json").exists()
    assert (out_dir / "toy-board-2026-04-24.score.json").exists()
    assert (out_dir / "_latest.json").exists()
    assert (pack_dir / "simulator_reliability.json").exists()
    # Summary
    assert result["n_accepted"] == 1
    assert result["n_rejected"] == 0


@pytest.mark.asyncio
async def test_end_to_end_mixed_batch(pack_dir, toy_graph, sample_draft, tmp_path):
    """One good, one topology reject, one dup — verify partitioning."""
    _write_graph(pack_dir, toy_graph)
    dup = sample_draft.model_dump()
    dup["local_id"] = "c19-short-dup"  # same (refdes, mode, rails) → V5 rejection
    bad = sample_draft.model_dump()
    bad["local_id"] = "bad-topo"
    bad["cause"] = {"refdes": "XZ999", "mode": "shorted"}

    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(
            _StubResponse(
                {
                    "scenarios": [sample_draft.model_dump(), dup, bad],
                }
            )
        )
    )
    out_dir = tmp_path / "auto_proposals"
    result = await generate_from_pack(
        device_slug="toy-board",
        client=client,
        model="claude-sonnet-4-6",
        memory_root=pack_dir.parent,
        output_dir=out_dir,
        latest_path=out_dir / "_latest.json",
        run_date="2026-04-24",
    )
    assert result["n_accepted"] == 1
    assert result["n_rejected"] == 2
    rejected = [
        json.loads(line)
        for line in (out_dir / "toy-board-2026-04-24.rejected.jsonl")
        .read_text()
        .strip()
        .split("\n")
    ]
    motives = {r["motive"] for r in rejected}
    assert "refdes_not_in_graph" in motives
    assert "duplicate_in_run" in motives
