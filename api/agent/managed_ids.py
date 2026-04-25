# SPDX-License-Identifier: Apache-2.0
"""Read the bootstrap IDs produced by `scripts/bootstrap_managed_agent.py`.

Supports both the multi-tier format (current) and the legacy single-agent
format (pre-C.5). The legacy agent, if present, is exposed as tier `deep`
with `legacy: True` so the runtime can decide whether to use it or skip to
the real deep agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

IDS_FILE = Path(__file__).resolve().parent.parent.parent / "managed_ids.json"


class AgentInfo(TypedDict, total=False):
    id: str
    version: int
    model: str
    legacy: bool


class ManagedIds(TypedDict):
    environment_id: str
    agents: dict[str, AgentInfo]  # keys: "fast" | "mid" | "deep"


def load_managed_ids() -> ManagedIds:
    """Return the persisted agent/environment IDs, tier-keyed.

    Raises `RuntimeError` if the file is missing. If the file is still in
    the legacy single-agent format, synthesises a single `deep` entry
    flagged `legacy: True` — the runtime can then upgrade via re-bootstrap.
    """
    if not IDS_FILE.exists():
        raise RuntimeError(
            f"{IDS_FILE.name} not found. Run "
            "`python scripts/bootstrap_managed_agent.py` before starting "
            "the diagnostic agent."
        )
    data: dict[str, Any] = json.loads(IDS_FILE.read_text())

    if "agents" in data:
        return {
            "environment_id": data["environment_id"],
            "agents": data["agents"],
        }

    # Legacy single-agent file — synthesize a deep-tier entry. The model
    # name is metadata only (the real model is bound at agent creation
    # server-side); we surface settings.anthropic_model_main so the local
    # echo stays in sync with the configured deep-tier choice.
    if "agent_id" in data:
        from api.config import get_settings
        return {
            "environment_id": data["environment_id"],
            "agents": {
                "deep": {
                    "id": data["agent_id"],
                    "version": data["agent_version"],
                    "model": get_settings().anthropic_model_main,
                    "legacy": True,
                }
            },
        }

    raise RuntimeError(
        f"{IDS_FILE.name} has an unknown shape (neither legacy nor multi-tier)."
    )


def get_agent(ids: ManagedIds, tier: str) -> AgentInfo:
    """Return the agent info for `tier`, or raise if missing."""
    agents = ids["agents"]
    if tier in agents:
        return agents[tier]
    raise RuntimeError(
        f"No agent bootstrapped for tier {tier!r}. "
        "Run `python scripts/bootstrap_managed_agent.py` to create it."
    )
