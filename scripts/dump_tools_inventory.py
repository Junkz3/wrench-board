#!/usr/bin/env python3
"""Auto-generate `docs/tools.md` from the diagnostic-agent tool manifest.

Reads the tool group lists declared in `api.agent.manifest` (`MB_TOOLS`,
`BV_TOOLS`, `PROFILE_TOOLS`, `PROTOCOL_TOOLS`, `CAM_TOOLS`,
`CONSULT_TOOLS`) and emits a Markdown reference grouping every tool by
family with its first-paragraph description and parameter table.

Idempotent: re-running with the manifest unchanged produces a
byte-identical output file (no wall-clock timestamp embedded). This is
intentional so `git diff` stays clean across re-generations and CI can
guard against drift.

Usage:
    .venv/bin/python scripts/dump_tools_inventory.py
    make tools-inventory
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from api.agent import manifest  # noqa: E402

# Ordered list of (group attr, family display label, short blurb).
GROUPS: list[tuple[str, str, str]] = [
    (
        "MB_TOOLS",
        "Memory bank (MB)",
        "Always-on. Memory-bank lookups, board aggregation, the schematic "
        "deterministic engines (`mb_schematic_graph`, `mb_hypothesize`), "
        "the per-repair measurement journal and the canonical archival API "
        "(`mb_record_finding`, `mb_record_session_log`, `mb_validate_finding`, "
        "`mb_expand_knowledge`).",
    ),
    (
        "BV_TOOLS",
        "Boardview (BV)",
        "Boardview rendering controls. Stripped from the manifest when the "
        "session has no board loaded (see `build_tools_manifest`).",
    ),
    (
        "PROFILE_TOOLS",
        "Technician profile",
        "Always-on. Read/check/track the technician's skills + tool "
        "inventory.",
    ),
    (
        "PROTOCOL_TOOLS",
        "Diagnostic protocol",
        "Always-on. Emit and steer a typed, stepwise diagnostic protocol "
        "rendered as floating cards on the board + side wizard.",
    ),
    (
        "CAM_TOOLS",
        "Camera",
        "Conditional. Exposed only when the frontend reported a camera "
        "available on session open.",
    ),
    (
        "CONSULT_TOOLS",
        "Consult specialist",
        "Managed-Agents only. Cross-tier escalation; absent from the "
        "DIRECT-mode manifest because direct mode runs a single "
        "`messages.create` loop with no peer tiers to dispatch to.",
    ),
]


def _short_description(text: str) -> str:
    """Collapse a multi-line description to its first sentence/paragraph.

    The full description is intentionally rich (used as the tool's prompt
    contract); the inventory keeps a one-liner for at-a-glance reading.
    """
    cleaned = " ".join(text.split())
    # Cut on first sentence terminator followed by space, keeping the period.
    for terminator in (". ", "? ", "! "):
        idx = cleaned.find(terminator)
        if 0 < idx < 220:
            return cleaned[: idx + 1].strip()
    if len(cleaned) > 220:
        return cleaned[:217].rstrip() + "..."
    return cleaned


def _format_type(prop: dict) -> str:
    """Render a JSON-schema property type for a Markdown table cell."""
    if "enum" in prop:
        values = ", ".join(f"`{v}`" for v in prop["enum"])
        return f"enum({values})"
    if "oneOf" in prop:
        return " \\| ".join(_format_type(p) for p in prop["oneOf"])
    t = prop.get("type")
    if isinstance(t, list):
        return " \\| ".join(t)
    if t == "array":
        items = prop.get("items") or {}
        inner = _format_type(items) if items else "any"
        return f"array<{inner}>"
    if t == "object":
        return "object"
    return t or "any"


def _params_table(input_schema: dict) -> list[str]:
    """Render the input_schema as a Markdown bullet list of params."""
    properties = input_schema.get("properties") or {}
    if not properties:
        return ["_no parameters_"]
    required = set(input_schema.get("required") or [])
    lines: list[str] = []
    lines.append("| Param | Type | Required | Description |")
    lines.append("|---|---|---|---|")
    for name, prop in properties.items():
        type_str = _format_type(prop)
        req = "yes" if name in required else "no"
        desc = (prop.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 180:
            desc = desc[:177].rstrip() + "..."
        # Pipe-escape so we don't break Markdown tables.
        desc_md = desc.replace("|", "\\|")
        lines.append(f"| `{name}` | {type_str} | {req} | {desc_md} |")
    return lines


def _render_family(group_attr: str, label: str, blurb: str) -> list[str]:
    tools = getattr(manifest, group_attr)
    out: list[str] = []
    out.append(f"## {label} — {len(tools)} tool(s)")
    out.append("")
    out.append(blurb)
    out.append("")
    for tool in tools:
        name = tool["name"]
        desc = _short_description(tool.get("description") or "")
        out.append(f"### `{name}`")
        out.append("")
        if desc:
            out.append(desc)
            out.append("")
        out.extend(_params_table(tool.get("input_schema") or {}))
        out.append("")
    return out


def render_inventory() -> str:
    """Build the full Markdown body deterministically."""
    lines: list[str] = []
    lines.append("# Tools manifest (auto-généré, ne pas éditer à la main)")
    lines.append("")
    lines.append(
        "Source de vérité : `api/agent/manifest.py`. Ce fichier est régénéré "
        "par `make tools-inventory` (ou directement "
        "`.venv/bin/python scripts/dump_tools_inventory.py`)."
    )
    lines.append("")
    lines.append(
        "Pas de timestamp embarqué : la sortie est déterministe pour rester "
        "diff-friendly entre deux régénérations à manifest constant. "
        "Si vous touchez un outil dans le manifest, régénérez ce fichier "
        "dans le même commit."
    )
    lines.append("")
    # Family summary table.
    lines.append("## Sommaire")
    lines.append("")
    lines.append("| Famille | Outils | Quand exposé |")
    lines.append("|---|---|---|")
    exposure = {
        "MB_TOOLS": "always",
        "BV_TOOLS": "session has a board",
        "PROFILE_TOOLS": "always",
        "PROTOCOL_TOOLS": "always",
        "CAM_TOOLS": "session reports a camera",
        "CONSULT_TOOLS": "Managed-Agents runtime only",
    }
    for group_attr, label, _blurb in GROUPS:
        tools = getattr(manifest, group_attr)
        lines.append(
            f"| {label} | {len(tools)} | {exposure.get(group_attr, '?')} |"
        )
    lines.append("")
    for group_attr, label, blurb in GROUPS:
        lines.extend(_render_family(group_attr, label, blurb))
    # Trailing newline for POSIX-friendly file ending.
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    target = REPO / "docs" / "tools.md"
    new_body = render_inventory()
    if target.exists() and target.read_text(encoding="utf-8") == new_body:
        print(f"[tools-inventory] up-to-date: {target.relative_to(REPO)}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_body, encoding="utf-8")
    print(f"[tools-inventory] wrote {target.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
