"""Direct-mode memory recall — pure read helpers backing three wrapper tools.

In managed mode the diagnostic agent greps three FUSE-mounted memory stores:
per-device field reports, global failure-pattern archetypes, and global
protocol playbooks. Direct mode (`runtime_direct`) has no FUSE mount, so without
these the agent is blind to that recall (see `field_reports.list_field_reports`
docstring: managed reads "via grep on the FUSE mount … rather than through a
wrapper tool"). These functions ARE that wrapper, exposed as `mb_recall_*` /
`mb_search_*` tools so the direct agent reaches parity.

All three are read-only and side-effect-free. Writes (recording findings,
saving protocols) already exist and are shared by both runtimes.

Matching is deliberately simple substring/keyword grep — the same shape as the
managed agent grepping the mounted files, not semantic search.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from api.agent.field_reports import list_field_reports

logger = logging.getLogger("wrench_board.agent.recall")

# Versioned seed data shipped with the engine (curated by hand). Same source the
# managed global stores are seeded from (see api/agent/seed_data/README.md).
_SEED_DIR = Path(__file__).resolve().parent / "seed_data"

# Default cap so a long device history can't blow up the agent's context.
_DEFAULT_FIELD_REPORT_LIMIT = 8


def recall_field_reports(
    *,
    device_slug: str,
    memory_root: Path | None = None,
    query: str | None = None,
    refdes: str | None = None,
    limit: int = _DEFAULT_FIELD_REPORT_LIMIT,
) -> list[dict[str, Any]]:
    """Recall confirmed field reports for THIS device, newest-first.

    Thin wrapper over `list_field_reports` (the disk-backed reader) that adds a
    free-text `query` filter (matched across every field) and caps the result.
    `refdes` is pushed down to the underlying reader. This is the direct-mode
    equivalent of the managed agent grepping the device field_reports store.
    """
    # Pull a generous window first (refdes pushed down), then keyword-filter and
    # cap here — so `query` narrows the newest-first set rather than the reader's
    # own `limit` truncating before we filter.
    reports = list_field_reports(
        device_slug=device_slug,
        memory_root=memory_root,
        limit=200,
        filter_refdes=refdes,
    )

    q = (query or "").lower().strip()
    if q:
        reports = [
            r for r in reports
            if q in " ".join(str(v) for v in r.values() if v is not None).lower()
        ]

    return reports[: max(limit, 0)]


def search_patterns(query: str, *, seed_dir: Path | None = None) -> list[dict[str, Any]]:
    """Search the global failure-pattern archetypes (markdown) by keyword.

    Returns `[{name, content}]` for every archetype whose filename or body
    contains `query` (case-insensitive substring) — the agent's "how do I reason
    about this kind of fault" recall. Archetypes are few and short, so the full
    body is returned for each hit.
    """
    base = (seed_dir or _SEED_DIR) / "global_patterns"
    if not base.exists():
        return []
    q = (query or "").lower().strip()
    if not q:
        return []

    hits: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("[Recall] unreadable pattern file: %s", path)
            continue
        if q in path.stem.lower() or q in content.lower():
            hits.append({"name": path.stem, "content": content})
    return hits


def search_playbooks(symptom: str, *, seed_dir: Path | None = None) -> list[dict[str, Any]]:
    """Search the global protocol playbooks (JSON) by symptom.

    Returns the full playbook dict (including `steps`) for every playbook whose
    `applies_when` overlaps `symptom` (case-insensitive substring either way) —
    so the agent can lift a validated step sequence before calling
    `bv_propose_protocol` instead of reinventing it.
    """
    base = (seed_dir or _SEED_DIR) / "global_playbooks"
    if not base.exists():
        return []
    s = (symptom or "").lower().strip()
    if not s:
        return []

    hits: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json")):
        try:
            pb = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("[Recall] unreadable/invalid playbook: %s", path)
            continue
        applies = [str(a).lower() for a in pb.get("applies_when", [])]
        if any(s in a or a in s for a in applies):
            hits.append(pb)
    return hits
