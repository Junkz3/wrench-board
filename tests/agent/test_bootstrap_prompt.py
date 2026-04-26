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


def test_tiers_models_pinned():
    """Tier → model mapping must stay in sync with the docstring + the
    runtime tier dispatch. fast=Haiku 4.5, normal=Sonnet 4.6, deep=Opus 4.7."""
    mod = _load_bootstrap_module()
    assert mod.TIERS["fast"]["model"] == "claude-haiku-4-5"
    assert mod.TIERS["normal"]["model"] == "claude-sonnet-4-6"
    assert mod.TIERS["deep"]["model"] == "claude-opus-4-7"


def test_agent_create_payload_omits_messages_api_only_fields():
    """Defense-in-depth: the MA `agents.create` payload must NOT contain
    Messages-API-only fields (`output_config`, `task_budget`, `effort`,
    `thinking`, `temperature`, `top_p`, `top_k`). Verified 2026-04 against
    `managed-agents-2026-04-01` + Python SDK 0.97.0 — MA's control plane
    does not surface these knobs (see bootstrap module docstring).

    If a future MA beta exposes one of these, update both this test AND
    the bootstrap module docstring before adding the field — silent
    addition would mask the regression on tiers that don't accept the
    parameter (e.g. effort=xhigh on Sonnet/Haiku 400s)."""
    mod = _load_bootstrap_module()
    forbidden = {
        "output_config", "task_budget", "effort", "thinking",
        "temperature", "top_p", "top_k",
    }
    for tier_name, spec in mod.TIERS.items():
        for key in forbidden:
            assert key not in spec, (
                f"Tier {tier_name!r} must not declare {key!r} — MA does not "
                "accept this Messages-API field. See bootstrap docstring."
            )
    for key in forbidden:
        assert key not in mod.CURATOR_SPEC, (
            f"CURATOR_SPEC must not declare {key!r} — MA does not "
            "accept this Messages-API field. See bootstrap docstring."
        )


def test_agent_toolset_uses_permission_policy_when_declared():
    """The diagnostic agent toolset disables web_*/bash by default for safety
    (prompt-injection attack surface). When the curator agent flips them on,
    each tool MUST carry an explicit permission_policy so the org-level
    admin gate is unambiguous about intent."""
    mod = _load_bootstrap_module()
    curator_toolset = mod.CURATOR_SPEC["tools"][0]
    assert curator_toolset["type"] == "agent_toolset_20260401"
    enabled_tools = [c for c in curator_toolset["configs"] if c.get("enabled")]
    for cfg in enabled_tools:
        assert "permission_policy" in cfg, (
            f"Curator tool {cfg['name']!r} is enabled but has no "
            "permission_policy — required for org-policy clarity."
        )
        assert cfg["permission_policy"]["type"] in {"always_allow", "always_ask"}
