# SPDX-License-Identifier: Apache-2.0
"""Cross-session memory for the diagnostic agent.

Each "field report" captures a confirmed finding from a technician — the refdes
that was actually at fault, the symptom the client reported, the mechanism, and
free-form notes. The next diagnostic session on the same device can read these
back to learn from prior repairs.

Two backends, same interface:

- **JSON (always on)** writes one Markdown file per report under
  `memory/{slug}/field_reports/{timestamp}-{refdes}.md`. Durable, audit-friendly,
  grep-able, and works without any Anthropic-side feature.
- **Managed Agents mirror (flag-gated)** additionally pushes the same content
  to the device's memory store when `settings.ma_memory_store_enabled=True` so
  the MA runtime can grep it on the `/mnt/memory/` filesystem mount. The JSON
  file is still written first — MA is a secondary accelerator, never the sole
  source of truth.

The split means cross-session learning is durable via the JSON path and
transparently accelerated through the MA memory store when the flag is on.
Zero migration when toggling the flag.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from api.agent.memory_stores import ensure_memory_store, upsert_memory
from api.config import get_settings

logger = logging.getLogger("wrench_board.agent.field_reports")


@dataclass
class FieldReport:
    """One confirmed finding, written to disk as a self-describing Markdown file.

    `report_id` is the filename stem (timestamp-refdes slug) so it's trivially
    dedupable and sortable without parsing YAML/JSON front-matter.
    """

    report_id: str
    device_slug: str
    refdes: str
    symptom: str
    confirmed_cause: str
    mechanism: str | None = None
    notes: str | None = None
    session_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_markdown(self) -> str:
        lines = [
            "---",
            f"report_id: {self.report_id}",
            f"device_slug: {self.device_slug}",
            f"refdes: {self.refdes}",
            f"symptom: {json.dumps(self.symptom, ensure_ascii=False)}",
            f"confirmed_cause: {json.dumps(self.confirmed_cause, ensure_ascii=False)}",
        ]
        if self.mechanism:
            lines.append(f"mechanism: {json.dumps(self.mechanism, ensure_ascii=False)}")
        if self.session_id:
            lines.append(f"session_id: {self.session_id}")
        lines.append(f"created_at: {self.created_at}")
        lines.append("---")
        lines.append("")
        lines.append(f"# {self.refdes} — {self.confirmed_cause}")
        lines.append("")
        lines.append(f"**Symptom observed:** {self.symptom}")
        lines.append("")
        lines.append(f"**Confirmed cause:** {self.confirmed_cause}")
        if self.mechanism:
            lines.append("")
            lines.append(f"**Failure mechanism:** {self.mechanism}")
        if self.notes:
            lines.append("")
            lines.append("## Notes")
            lines.append("")
            lines.append(self.notes)
        return "\n".join(lines) + "\n"


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
_YAML_LINE_RE = re.compile(r"^(\w+):\s*(.*)$")


def _parse_report(path: Path) -> FieldReport | None:
    """Parse a Markdown report back into a FieldReport. Returns None on malformed input.

    The frontmatter is the primary source of truth; the prose body is advisory
    (human-readable, not machine-consumed here).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return None
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        m = _YAML_LINE_RE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        meta[key] = value
    try:
        return FieldReport(
            report_id=meta["report_id"],
            device_slug=meta["device_slug"],
            refdes=meta["refdes"],
            symptom=meta["symptom"],
            confirmed_cause=meta["confirmed_cause"],
            mechanism=meta.get("mechanism") or None,
            session_id=meta.get("session_id") or None,
            created_at=meta.get("created_at")
            or datetime.now(UTC).isoformat(),
        )
    except KeyError:
        return None


def _slug_fragment(text: str, max_len: int = 32) -> str:
    """URL / filename safe fragment of `text`, trimmed to `max_len`."""
    frag = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip().lower())
    frag = re.sub(r"-+", "-", frag).strip("-")
    return (frag or "unknown")[:max_len]


def _reports_dir(device_slug: str, memory_root: Path) -> Path:
    return memory_root / device_slug / "field_reports"


async def record_field_report(
    *,
    client: AsyncAnthropic | None,
    device_slug: str,
    refdes: str,
    symptom: str,
    confirmed_cause: str,
    mechanism: str | None = None,
    notes: str | None = None,
    session_id: str | None = None,
    memory_root: Path | None = None,
) -> dict[str, Any]:
    """Write a new field report. JSON-first; MA mirror when the flag is on.

    Returns a status dict — tests and telemetry both key on it. Never raises:
    MA mirror failure does NOT fail the JSON write; the audit record stands.
    """
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)

    created_at = datetime.now(UTC)
    # Filename: ISO timestamp in compact form + refdes slug — sortable and
    # dedupable. Second resolution is enough; if two reports race at the same
    # second for the same refdes, the second one overwrites (acceptable — both
    # carry identical content if they're truly duplicate, and idempotent writes
    # simplify the MA mirror path too).
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    report_id = f"{stamp}-{_slug_fragment(refdes)}"

    report = FieldReport(
        report_id=report_id,
        device_slug=device_slug,
        refdes=refdes,
        symptom=symptom,
        confirmed_cause=confirmed_cause,
        mechanism=mechanism,
        notes=notes,
        session_id=session_id,
        created_at=created_at.isoformat(),
    )
    markdown = report.to_markdown()

    reports_dir = _reports_dir(device_slug, memory_root)
    reports_dir.mkdir(parents=True, exist_ok=True)
    file_path = reports_dir / f"{report_id}.md"
    file_path.write_text(markdown, encoding="utf-8")
    logger.info(
        "[FieldReport] Wrote slug=%s refdes=%s report_id=%s",
        device_slug,
        refdes,
        report_id,
    )

    status: dict[str, Any] = {
        "report_id": report_id,
        "json_path": str(file_path),
        "json_status": "written",
        "ma_mirror_status": "skipped:flag_disabled",
    }

    if not settings.ma_memory_store_enabled:
        return status
    if client is None:
        status["ma_mirror_status"] = "skipped:no_client"
        return status

    status["ma_mirror_status"] = await _mirror_to_managed_agents(
        client=client,
        device_slug=device_slug,
        report_id=report_id,
        markdown=markdown,
    )
    return status


async def _mirror_to_managed_agents(
    *,
    client: AsyncAnthropic,
    device_slug: str,
    report_id: str,
    markdown: str,
) -> str:
    """Mirror one report to the device's MA memory store. Returns a status string."""
    store_id = await ensure_memory_store(client, device_slug)
    if store_id is None:
        return "skipped:no_store"

    result = await upsert_memory(
        client,
        store_id=store_id,
        path=f"/field_reports/{report_id}.md",
        content=markdown,
    )
    if result is None:
        logger.warning(
            "[FieldReport] MA mirror failed for slug=%s report_id=%s",
            device_slug,
            report_id,
        )
        return "error:upsert_failed"

    return "mirrored"


def list_field_reports(
    *,
    device_slug: str,
    memory_root: Path | None = None,
    limit: int = 20,
    filter_refdes: str | None = None,
) -> list[dict[str, Any]]:
    """Return reports sorted newest-first, filtered by refdes when supplied.

    Pure disk read — the JSON-backed path that works without MA access.
    Used by the `/pipeline/packs/{slug}/findings` HTTP endpoint and as a
    test helper. The diagnostic agent reads the same content via grep on
    the FUSE mount (`/mnt/memory/wrench-board-{slug}/field_reports/`)
    rather than through a wrapper tool.
    """
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    reports_dir = _reports_dir(device_slug, memory_root)
    if not reports_dir.exists():
        return []

    reports: list[FieldReport] = []
    for path in reports_dir.glob("*.md"):
        report = _parse_report(path)
        if report is None:
            logger.warning("[FieldReport] Skipping malformed report: %s", path)
            continue
        if filter_refdes and report.refdes != filter_refdes:
            continue
        reports.append(report)

    reports.sort(key=lambda r: r.created_at, reverse=True)
    reports = reports[: max(limit, 0)]
    return [
        {
            "report_id": r.report_id,
            "device_slug": r.device_slug,
            "refdes": r.refdes,
            "symptom": r.symptom,
            "confirmed_cause": r.confirmed_cause,
            "mechanism": r.mechanism,
            "notes": r.notes,
            "session_id": r.session_id,
            "created_at": r.created_at,
        }
        for r in reports
    ]
