"""Phase 2 — Registry Builder. Forced-tool output, Pydantic-validated.

Converts the Scout's raw Markdown dump into a canonical `Registry` JSON.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from api.pipeline.prompts import REGISTRY_SYSTEM, REGISTRY_USER_TEMPLATE
from api.pipeline.schemas import Registry
from api.pipeline.tool_call import call_with_forced_tool

if TYPE_CHECKING:
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("microsolder.pipeline.registry")


SUBMIT_REGISTRY_TOOL_NAME = "submit_registry"


def _submit_registry_tool() -> dict:
    """Build the forced-tool definition whose `input_schema` matches `Registry`."""
    schema = Registry.model_json_schema()
    return {
        "name": SUBMIT_REGISTRY_TOOL_NAME,
        "description": (
            "Submit the canonical glossary of components and signals for the device. "
            "This is your only valid form of output."
        ),
        "input_schema": schema,
    }


async def run_registry_builder(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    raw_dump: str,
    stats: PhaseTokenStats | None = None,
) -> Registry:
    """Execute Phase 2 — return a validated `Registry` Pydantic model."""
    logger.info("[Registry] Building canonical glossary for device=%r", device_label)

    user_prompt = REGISTRY_USER_TEMPLATE.format(
        device_label=device_label,
        raw_dump=raw_dump,
    )

    registry = await call_with_forced_tool(
        client=client,
        model=model,
        system=REGISTRY_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[_submit_registry_tool()],
        forced_tool_name=SUBMIT_REGISTRY_TOOL_NAME,
        output_schema=Registry,
        max_attempts=2,
        log_label="Registry",
        stats=stats,
    )

    logger.info(
        "[Registry] Built · components=%d signals=%d",
        len(registry.components),
        len(registry.signals),
    )
    return registry
