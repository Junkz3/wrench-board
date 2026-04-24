# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from api.pipeline.bench_generator.prompts import (
    SYSTEM_PROMPT,
    build_user_message,
    graph_summary,
)


def test_system_prompt_mentions_grounding_contract():
    assert "evidence" in SYSTEM_PROMPT.lower()
    assert "verbatim" in SYSTEM_PROMPT.lower() or "literal" in SYSTEM_PROMPT.lower()


def test_graph_summary_lists_components_and_rails(toy_graph):
    summary = graph_summary(toy_graph)
    assert "U7" in summary
    assert "+3V3" in summary
    assert "voltage_nominal" in summary
    # Edges are explicitly NOT included
    assert "edges" not in summary


def test_build_user_message_composes_four_blocks(toy_graph):
    msg = build_user_message(
        raw_dump="## Scout dump\n\nC19 shorts collapse +3V3.",
        rules_json='{"rules": []}',
        registry_json='{"components": []}',
        graph=toy_graph,
    )
    assert "Scout dump" in msg
    assert "C19 shorts" in msg
    assert '"rules"' in msg
    assert '"components"' in msg
    assert "U7" in msg  # from graph summary
    assert "Device slug: toy-board" in msg
