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


def test_functional_candidate_map_prefers_registry_refdes_candidates(toy_graph):
    """When a registry component carries refdes_candidates, the functional
    map sources its candidates from there instead of running the rail-overlap
    heuristic. The source label flips to 'registry'."""
    import json

    from api.pipeline.bench_generator.prompts import build_functional_candidate_map

    registry = {
        "components": [
            {
                "canonical_name": "main buck",
                "aliases": ["buck"],
                "kind": "ic",
                "description": "main buck regulator",
                "refdes_candidates": [
                    {
                        "refdes": "U7",
                        "confidence": 0.95,
                        "evidence": "dump quote 'LM2677 buck (U7)' ties canonical to U7",
                    }
                ],
            },
            # Legacy entry without refdes_candidates → still flows through heuristic.
            {
                "canonical_name": "+3V3 regulator",
                "aliases": ["3V3 regulator"],
                "kind": "ic",
                "description": "sources +3V3",
            },
        ]
    }
    out = build_functional_candidate_map(registry, toy_graph)
    # Registry-sourced canonical is labelled and uses the LLM-supplied evidence.
    main_buck_block = out.split("### main buck")[1].split("###")[0]
    assert "source: registry" in main_buck_block
    assert "U7 (score=0.95)" in main_buck_block
    assert "ties canonical to U7" in main_buck_block
    # Legacy canonical falls back to the heuristic — source label says so.
    legacy_block = out.split("### +3V3 regulator")[1]
    assert "source: heuristic" in legacy_block
    # Make sure the registry block didn't leak heuristic vocabulary.
    assert "rail-overlap" not in main_buck_block
    # Sanity: round-trip through json — registry shape must serialize cleanly.
    json.dumps(registry)
