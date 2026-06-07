from api.agent.manifest import (
    STOCK_TOOLS,
    build_tools_manifest,
    render_system_prompt,
)
from api.session.state import SessionState


def test_stock_tools_count_and_names():
    names = {t["name"] for t in STOCK_TOOLS}
    assert names == {
        "stock_search", "stock_consume", "stock_mark_donor",
        "stock_unmark_donor", "stock_list_donors",
    }


def test_stock_tools_have_input_schema():
    for t in STOCK_TOOLS:
        assert t["type"] == "custom"
        assert "input_schema" in t
        assert t["input_schema"]["type"] == "object"


def test_build_tools_manifest_includes_stock_tools_always():
    session = SessionState()  # no board loaded
    manifest = build_tools_manifest(session)
    names = {t["name"] for t in manifest}
    for tn in ("stock_search", "stock_consume", "stock_mark_donor",
               "stock_unmark_donor", "stock_list_donors"):
        assert tn in names


def test_system_prompt_contains_stock_awareness_block():
    session = SessionState()
    prompt = render_system_prompt(session, device_slug="iphone-x")
    assert "stock_search" in prompt.lower() or "stock awareness" in prompt.lower()
    assert "stock" in prompt.lower()
