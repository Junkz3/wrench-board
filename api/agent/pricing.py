# SPDX-License-Identifier: Apache-2.0
"""Per-turn token cost estimation for diagnostic sessions.

Pricing is tracked by model family. Cache semantics apply: cache_read
inputs bill at 10% of the base input rate, cache_creation at 125%.

Numbers are best-effort April 2026 estimates — they feed the "cost per
message / total conversation" surface the tech sees in the chat panel.
If Anthropic changes prices, bump the table here. Off by a cent or two
is fine — the goal is to let the tech reason about token spend live,
not to reconcile invoices.
"""

from __future__ import annotations

from typing import Any

# Per-million-token rates in USD, sourced from
# https://platform.claude.com/docs/en/about-claude/pricing (April 2026).
# Opus dropped from the legacy $15/$75 tier at Opus 4.5; 4.7 stays at $5/$25.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5":  {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00},
}

# Cache tier multipliers applied to the base input rate.
CACHE_READ_MULTIPLIER  = 0.10
CACHE_WRITE_MULTIPLIER = 1.25


def compute_turn_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> dict[str, Any]:
    """Return the USD cost breakdown for one turn.

    `input_tokens` is the billable non-cached input; `cache_read_input_tokens`
    and `cache_creation_input_tokens` are priced at their own multipliers
    and should NOT be double-counted in `input_tokens`. This matches the
    Anthropic usage shape.
    """
    rates = MODEL_PRICING.get(model)
    if rates is None:
        return {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cost_usd": 0.0,
            "priced": False,
        }
    base_in  = rates["input"]
    base_out = rates["output"]
    cost = 0.0
    cost += (input_tokens  / 1_000_000.0) * base_in
    cost += (output_tokens / 1_000_000.0) * base_out
    cost += (cache_read_input_tokens     / 1_000_000.0) * base_in * CACHE_READ_MULTIPLIER
    cost += (cache_creation_input_tokens / 1_000_000.0) * base_in * CACHE_WRITE_MULTIPLIER
    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cost_usd": round(cost, 6),
        "priced": True,
    }


def cost_from_response(model: str, usage: Any) -> dict[str, Any]:
    """Compute cost from an anthropic `Message.usage` object."""
    return compute_turn_cost(
        model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )
