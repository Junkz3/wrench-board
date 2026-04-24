"""Tests for sanitize_agent_text — post-hoc refdes guard."""

from api.agent.sanitize import sanitize_agent_text
from api.board.model import Board, Layer, Part, Point


def _board_with_parts(refdeses: list[str]) -> Board:
    parts = [
        Part(
            refdes=r,
            layer=Layer.TOP,
            is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=10)),
            pin_refs=[],
        )
        for r in refdeses
    ]
    return Board(
        board_id="test", file_hash="sha256:x", source_format="test",
        outline=[], parts=parts, pins=[], nets=[], nails=[],
    )


def test_noop_when_board_is_none() -> None:
    text = "Check U7 and U999 please"
    clean, unknown = sanitize_agent_text(text, None)
    assert clean == text
    assert unknown == []


def test_wraps_unknown_refdes_and_keeps_known() -> None:
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text("Check U7 and U999 please", board)
    assert clean == "Check U7 and ⟨?U999⟩ please"
    assert unknown == ["U999"]


def test_multiple_unknown_refdes_all_wrapped() -> None:
    board = _board_with_parts(["C1"])
    clean, unknown = sanitize_agent_text("U1, U2, C1, R3 are suspect", board)
    assert "⟨?U1⟩" in clean
    assert "⟨?U2⟩" in clean
    assert "C1" in clean  # known, not wrapped
    assert "⟨?R3⟩" in clean
    assert set(unknown) == {"U1", "U2", "R3"}


def test_does_not_match_net_names_with_underscore() -> None:
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("HDMI_D0 and VDD_3V3 are rails", board)
    assert clean == "HDMI_D0 and VDD_3V3 are rails"
    assert unknown == []


def test_does_not_match_lowercase() -> None:
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("the u7 part is mentioned", board)
    assert clean == "the u7 part is mentioned"
    assert unknown == []


def test_flags_refdes_shaped_protocol_names() -> None:
    """Tokens like USB3 match the pattern; flagged when absent. Known limitation."""
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("USB3 is fine", board)
    assert clean == "⟨?USB3⟩ is fine"
    assert unknown == ["USB3"]


def test_empty_text() -> None:
    board = _board_with_parts(["U1"])
    clean, unknown = sanitize_agent_text("", board)
    assert clean == ""
    assert unknown == []


def test_refdes_at_string_boundaries() -> None:
    board = _board_with_parts(["U1"])
    clean, unknown = sanitize_agent_text("U999", board)
    assert clean == "⟨?U999⟩"
    assert unknown == ["U999"]
    clean, unknown = sanitize_agent_text("U1", board)
    assert clean == "U1"
    assert unknown == []


# --- Edge-case contexts ------------------------------------------------------
# The sanitizer is intentionally context-free — it runs one regex over the
# whole message with no Markdown, HTML, URL or JSON parsing. These tests lock
# in that behavior so future refactors don't silently start (or stop) wrapping
# refdes tokens depending on surrounding syntax.


def test_wraps_inside_markdown_triple_fence() -> None:
    """Code fences don't hide unknown refdes: the agent might still make a
    claim ("U999 is shorted") inside a code example, and we want the tech
    to see the ⟨?⟩ warning regardless."""
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text("```\ncheck U999\n```", board)
    assert "⟨?U999⟩" in clean
    assert unknown == ["U999"]


def test_wraps_inside_inline_backticks() -> None:
    """Inline `U999` — same policy as fenced block: wrap if unknown."""
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text("the `U999` chip", board)
    assert "⟨?U999⟩" in clean
    assert unknown == ["U999"]


def test_wraps_in_url_fragment() -> None:
    """A URL fragment matching the refdes shape is wrapped. Mangles the URL
    but the agent shouldn't be emitting refdes-shaped URL fragments anyway;
    safety trumps link fidelity."""
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text(
        "see http://example.com/page#U999 for details", board
    )
    assert "⟨?U999⟩" in clean
    assert unknown == ["U999"]


def test_wraps_hex_color_shape_as_known_limitation() -> None:
    """All-caps hex colors like #ABC123 match the refdes regex and get
    wrapped. Documented limitation — same class as USB3. The sanitizer has
    no way to know ABC123 is meant as a color."""
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text("background: #ABC123;", board)
    assert "⟨?ABC123⟩" in clean
    assert unknown == ["ABC123"]


def test_ignores_lowercase_hex_color() -> None:
    """Lowercase hex #abc123 escapes the regex (which requires [A-Z]) — the
    common CSS convention stays unwrapped."""
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("background: #abc123;", board)
    assert clean == "background: #abc123;"
    assert unknown == []


def test_wraps_html_attribute_value() -> None:
    """HTML id="U999" → U999 is wrapped. No attribute-context awareness."""
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text('<span id="U999">x</span>', board)
    assert "⟨?U999⟩" in clean
    assert unknown == ["U999"]


def test_wraps_json_key() -> None:
    """A refdes-shaped token used as a JSON key still gets wrapped — which
    breaks the JSON, intentionally. Agent output shouldn't carry literal
    JSON payloads keyed by refdes."""
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text('{"U999": "bad"}', board)
    assert "⟨?U999⟩" in clean
    assert unknown == ["U999"]


def test_handles_adjacent_punctuation() -> None:
    """Word-boundary anchors isolate the token from trailing punctuation so
    the wrap doesn't swallow the period, comma, parenthesis, etc."""
    board = _board_with_parts([])
    for sample in (
        "U999.",
        "U999,",
        "U999!",
        "U999?",
        "(U999)",
        "[U999]",
        "U999:",
        '"U999"',
        "« U999 »",
    ):
        clean, unknown = sanitize_agent_text(sample, board)
        assert "⟨?U999⟩" in clean, f"failed on {sample!r}: got {clean!r}"
        assert unknown == ["U999"], f"failed on {sample!r}"


def test_refdes_over_four_digits_is_not_wrapped() -> None:
    """Known limitation: the regex caps at \\d{1,4}, so a hallucinated U10000
    slips through. Real boards rarely exceed 4-digit refdes so this is
    accepted; lock in the behavior so a regex tweak is a deliberate decision."""
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("check U10000", board)
    assert clean == "check U10000"
    assert unknown == []


def test_comma_separated_list_wraps_each_token() -> None:
    """A comma-separated list of refdes is scanned token-by-token, with
    known ones preserved and unknown ones individually wrapped."""
    board = _board_with_parts(["U1"])
    clean, unknown = sanitize_agent_text("U999,U1000,U1", board)
    assert "⟨?U999⟩" in clean
    assert "⟨?U1000⟩" in clean
    assert "U1" in clean
    assert "⟨?U1⟩" not in clean
    assert set(unknown) == {"U999", "U1000"}


def test_french_prose_context() -> None:
    """Sanitizer runs on French agent output just as well as English — the
    regex is language-agnostic."""
    board = _board_with_parts(["C1"])
    clean, unknown = sanitize_agent_text(
        "Vérifier U999 et C1, puis mesurer le rail.", board
    )
    assert "⟨?U999⟩" in clean
    assert "C1" in clean
    assert "⟨?C1⟩" not in clean
    assert unknown == ["U999"]
