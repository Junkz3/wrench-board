"""Deterministic device-facet extraction (T9a). Pure, fast — no LLM."""
from api.pipeline.device_identity import (
    STRONG_KINDS,
    extract_facets,
    normalize_token,
)


def _by_kind(facets):
    out = {}
    for f in facets:
        out.setdefault(f["kind"], []).append(f["value"])
    return out


def test_extracts_board_apple_model_and_codename():
    facets = _by_kind(extract_facets("A1286_820-2533 K19i"))
    assert facets["board"] == ["820-2533"]
    assert facets["apple_model"] == ["A1286"]
    assert facets["codename"] == ["K19i"]
    # The whole label is always kept as a searchable marketing facet.
    assert "A1286_820-2533 K19i" in facets["marketing"]


def test_marketing_only_label_has_no_board():
    facets = _by_kind(extract_facets("MacBook Pro A1286"))
    assert facets["apple_model"] == ["A1286"]
    assert "board" not in facets
    assert facets["marketing"] == ["MacBook Pro A1286"]


def test_bare_board_number():
    facets = _by_kind(extract_facets("820-2533"))
    assert facets["board"] == ["820-2533"]


def test_extracts_emc_normalized():
    facets = _by_kind(extract_facets("iPhone X A1901 EMC 3164"))
    assert facets["apple_model"] == ["A1901"]
    assert facets["emc"] == ["EMC 3164"]


def test_board_and_emc_are_strong_kinds():
    assert STRONG_KINDS == {"board", "emc"}


def test_normalize_token_is_match_stable():
    assert normalize_token('MacBook Pro 15" 2011') == "macbook pro 15 2011"
    assert normalize_token("820-2533") == normalize_token("820 2533")


def test_no_false_board_from_emc_suffix():
    # "2353-1" must not be mistaken for a board number (board = 8xx-xxxx).
    facets = _by_kind(extract_facets("EMC 2353-1"))
    assert "board" not in facets
    assert facets["emc"] == ["EMC 2353"]


def test_dedupes_repeated_tokens():
    facets = _by_kind(extract_facets("820-2533 820-2533 A1286"))
    assert facets["board"] == ["820-2533"]
    assert facets["apple_model"] == ["A1286"]
