import importlib.util
from pathlib import Path


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_managed_agent",
        Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_managed_agent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_system_prompt_has_layered_memory_block():
    """Replaces the old bimodal (mount-vs-disk) test — the prompt now
    describes the 4-layer architecture (global patterns + global playbooks +
    device + repair) and the scribe discipline for the per-repair mount."""
    mod = _load_bootstrap_module()
    prompt = mod.SYSTEM_PROMPT
    # 4 layers must be named so the agent knows what to grep where.
    assert "/mnt/memory/" in prompt
    assert "global-patterns" in prompt
    assert "global-playbooks" in prompt
    assert "scribe" in prompt.lower()
    # Must NOT mention the deprecated tool / mode.
    assert "mb_list_findings" not in prompt
    assert "Mode disk-only" not in prompt


def test_system_prompt_has_grep_example():
    """Concrete grep usage so the agent has a pattern to imitate when
    consulting the mount layers (global patterns, device field_reports, etc.)."""
    mod = _load_bootstrap_module()
    prompt = mod.SYSTEM_PROMPT
    assert "grep -r" in prompt or 'grep "' in prompt or "grep " in prompt
