import pytest
from pydantic import ValidationError
from api.pipeline.board_delta.schemas import DeltaBoard


def test_minimal_none_coverage_is_valid():
    d = DeltaBoard(device_label="MacBook Air M1", board_number="820-02016", coverage="none")
    assert d.schema_version == "1.0"
    assert d.signature_ics == []
    assert d.is_empty() is True


def test_rich_delta_roundtrips():
    d = DeltaBoard(
        device_label="MacBook Air M1",
        board_number="820-02016",
        coverage="rich",
        signature_ics=[{"part": "ISL9240", "role": "charger", "source_url": "http://x"}],
        repair_pitfalls=[{"title": "no power", "detail": "PP3v8 at 0.7V", "source_url": "http://y"}],
        sources=[{"url": "http://x", "kind": "forum"}],
        generated_at="2026-06-16T00:00:00Z",
        generated_by_tenant="t1",
    )
    assert d.is_empty() is False
    assert d.signature_ics[0].part == "ISL9240"


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        DeltaBoard(device_label="x", board_number="y", coverage="none", bogus=1)


def test_bad_coverage_rejected():
    with pytest.raises(ValidationError):
        DeltaBoard(device_label="x", board_number="y", coverage="maybe")


def test_is_empty_ignores_kinship_hints():
    """kinship_hints are seeds for chantier 3, not diagnostic content.

    A DeltaBoard with only kinship_hints must be considered empty so the
    honesty rule downgrades coverage to 'none' and build_board_delta_block
    returns None (no content-free block is injected into the agent store).
    """
    d = DeltaBoard(
        device_label="MacBook Air M1",
        board_number="820-02016",
        coverage="thin",
        kinship_hints=[
            {"board_number": "820-02007", "relation": "predecessor Intel variant", "source_url": "http://z"}
        ],
    )
    assert d.is_empty() is True
