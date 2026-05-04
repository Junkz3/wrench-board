"""Read the bootstrap IDs produced by `scripts/bootstrap_managed_agent.py`.

The expected on-disk shape is the multi-tier format
(`{"environment_id", "agents": {"fast", "normal", "deep"}}`). The bootstrap
script also migrates pre-multi-tier files in place when they're detected, so
the runtime here can stay narrow and reject anything that hasn't been
upgraded yet.
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
    agents: dict[str, AgentInfo]  # keys: "fast" | "normal" | "deep"


def load_managed_ids() -> ManagedIds:
    """Return the persisted agent/environment IDs, tier-keyed.

    Raises `RuntimeError` if the file is missing or in an unrecognised shape;
    the caller is expected to re-run `scripts/bootstrap_managed_agent.py` to
    materialise / migrate it.
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

    raise RuntimeError(
        f"{IDS_FILE.name} has an unrecognised shape — re-run "
        "`python scripts/bootstrap_managed_agent.py` to migrate it."
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
