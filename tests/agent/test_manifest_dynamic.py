"""Tests for build_tools_manifest and render_system_prompt."""

from api.agent.manifest import BV_TOOLS, MB_TOOLS, build_tools_manifest, render_system_prompt
from api.board.model import Board, Layer, Part, Point
from api.session.state import SessionState


def _session_with_board() -> SessionState:
    parts = [Part(refdes="U7", layer=Layer.TOP, is_smd=True,
                  bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[])]
    board = Board(board_id="b", file_hash="sha256:x", source_format="t",
                  outline=[], parts=parts, pins=[], nets=[], nails=[])
    s = SessionState()
    s.set_board(board)
    return s


def test_mb_tools_contains_core_four_plus_expand() -> None:
    # The core four are the lookup/record set this chantier shipped. A fifth
    # tool (mb_expand_knowledge) was added out-of-band — we assert the core
    # set is present without pinning an exact count, so future growth is
    # allowed but regressions on the four foundational names still trip.
    names = {t["name"] for t in MB_TOOLS}
    core = {
        "mb_get_component", "mb_get_rules_for_symptoms",
        "mb_list_findings", "mb_record_finding",
    }
    assert core.issubset(names)


def test_bv_tools_has_thirteen_entries() -> None:
    assert len(BV_TOOLS) == 13
    names = {t["name"] for t in BV_TOOLS}
    assert names == {
        "bv_highlight", "bv_focus", "bv_reset_view", "bv_flip",
        "bv_annotate", "bv_dim_unrelated", "bv_highlight_net",
        "bv_show_pin", "bv_draw_arrow", "bv_measure",
        "bv_filter_by_type", "bv_layer_visibility", "bv_scene",
    }


def test_every_tool_has_name_description_input_schema() -> None:
    from api.agent.manifest import PROFILE_TOOLS
    for tool in MB_TOOLS + BV_TOOLS + PROFILE_TOOLS:
        assert isinstance(tool["name"], str) and tool["name"]
        assert isinstance(tool["description"], str) and tool["description"]
        assert isinstance(tool["input_schema"], dict)
        assert tool["input_schema"].get("type") == "object"
        assert "properties" in tool["input_schema"]


def test_no_description_exceeds_managed_agents_limit() -> None:
    # Anthropic Managed Agents reject tool definitions whose description
    # exceeds 1024 characters; the bootstrap script silently *skips* such
    # tools, so the agent loses the capability without an obvious failure.
    # Regression-guard the budget at manifest-load time.
    MA_DESCRIPTION_LIMIT = 1024
    from api.agent.manifest import PROFILE_TOOLS, PROTOCOL_TOOLS
    overflows = [
        (t["name"], len(t["description"]))
        for t in MB_TOOLS + BV_TOOLS + PROFILE_TOOLS + PROTOCOL_TOOLS
        if len(t["description"]) > MA_DESCRIPTION_LIMIT
    ]
    assert overflows == [], (
        f"tool descriptions over the MA {MA_DESCRIPTION_LIMIT}-char limit: "
        f"{overflows}"
    )


def test_manifest_without_board_has_only_mb_profile_protocol_tools() -> None:
    from api.agent.manifest import PROFILE_TOOLS, PROTOCOL_TOOLS
    session = SessionState()  # board=None
    manifest = build_tools_manifest(session)
    names = {t["name"] for t in manifest}
    expected = (
        {t["name"] for t in MB_TOOLS}
        | {t["name"] for t in PROFILE_TOOLS}
        | {t["name"] for t in PROTOCOL_TOOLS}
    )
    assert names == expected
    assert len(manifest) == len(MB_TOOLS) + len(PROFILE_TOOLS) + len(PROTOCOL_TOOLS)


def test_manifest_with_board_adds_bv_tools() -> None:
    from api.agent.manifest import PROFILE_TOOLS, PROTOCOL_TOOLS
    session = _session_with_board()
    manifest = build_tools_manifest(session)
    names = {t["name"] for t in manifest}
    expected = (
        {t["name"] for t in MB_TOOLS}
        | {t["name"] for t in BV_TOOLS}
        | {t["name"] for t in PROFILE_TOOLS}
        | {t["name"] for t in PROTOCOL_TOOLS}
    )
    assert names == expected
    assert len(manifest) == len(MB_TOOLS) + len(BV_TOOLS) + len(PROFILE_TOOLS) + len(PROTOCOL_TOOLS)


def test_manifest_has_no_sch_tools_regardless_of_session() -> None:
    session = _session_with_board()
    manifest = build_tools_manifest(session)
    assert not any(t["name"].startswith("sch_") for t in manifest)


def test_render_system_prompt_mentions_boardview_when_available() -> None:
    session = _session_with_board()
    prompt = render_system_prompt(session, device_slug="demo-pi")
    assert "boardview" in prompt.lower()
    assert "demo-pi" in prompt


def test_render_system_prompt_mentions_boardview_absent_when_no_board() -> None:
    session = SessionState()
    prompt = render_system_prompt(session, device_slug="demo-pi")
    assert "boardview" in prompt.lower()
    assert "memory bank" in prompt.lower()


def test_bv_highlight_refdes_accepts_string_or_array() -> None:
    """Req 6 — oneOf schema for refdes param."""
    schema = next(t for t in BV_TOOLS if t["name"] == "bv_highlight")["input_schema"]
    refdes_schema = schema["properties"]["refdes"]
    assert "oneOf" in refdes_schema
    types = {s["type"] for s in refdes_schema["oneOf"]}
    assert types == {"string", "array"}


def test_enum_constraints_present() -> None:
    """Req 7 — color and layer fields declare enum constraint."""
    bv_h = next(t for t in BV_TOOLS if t["name"] == "bv_highlight")["input_schema"]
    assert "enum" in bv_h["properties"]["color"]
    assert set(bv_h["properties"]["color"]["enum"]) == {"accent", "warn", "mute"}
    bv_lv = next(t for t in BV_TOOLS if t["name"] == "bv_layer_visibility")["input_schema"]
    assert "enum" in bv_lv["properties"]["layer"]
    assert set(bv_lv["properties"]["layer"]["enum"]) == {"top", "bottom"}


def test_bv_show_pin_minimum() -> None:
    """Req 8 — pin index must be >= 1."""
    schema = next(t for t in BV_TOOLS if t["name"] == "bv_show_pin")["input_schema"]
    assert schema["properties"]["pin"].get("minimum") == 1


def test_mb_list_findings_limit_constraints() -> None:
    """Req 9 — limit range 1..100."""
    schema = next(t for t in MB_TOOLS if t["name"] == "mb_list_findings")["input_schema"]
    limit = schema["properties"]["limit"]
    assert limit.get("minimum") == 1
    assert limit.get("maximum") == 100


def test_profile_tools_always_present() -> None:
    names_no_board = {t["name"] for t in build_tools_manifest(SessionState())}
    assert {"profile_get", "profile_check_skills", "profile_track_skill"} <= names_no_board

    names_with_board = {t["name"] for t in build_tools_manifest(_session_with_board())}
    assert {"profile_get", "profile_check_skills", "profile_track_skill"} <= names_with_board


def test_protocol_tools_in_manifest() -> None:
    from api.agent.manifest import PROTOCOL_TOOLS
    names = {t["name"] for t in PROTOCOL_TOOLS}
    assert names == {
        "bv_propose_protocol",
        "bv_update_protocol",
        "bv_record_step_result",
        "bv_get_protocol",
    }
    for t in PROTOCOL_TOOLS:
        assert len(t["description"]) <= 1024  # MA cap
        assert "input_schema" in t


def test_render_system_prompt_includes_protocol_section() -> None:
    from api.agent.manifest import render_system_prompt
    from api.session.state import SessionState
    out = render_system_prompt(SessionState(), device_slug="demo")
    assert "PROTOCOLE" in out or "protocol" in out.lower()
    assert "bv_propose_protocol" in out
