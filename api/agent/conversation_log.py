"""Cross-conversation narrative log for the diagnostic agent.

A field_report (`api/agent/field_reports.py`) is component-grain: "I confirmed
U1501 was at fault on this device." A *session log* is conversation-grain:
"On the 22/04 chat for repair R1, we tested PP3V0 + PP1V8, ruled out U1501,
left it on suspect U1700 — paused because the tech was waiting on a part."

Field reports answer 'has anyone here ever blamed this refdes?'. Session
logs answer 'did we already test this rail / explore this hypothesis on
this device, in any past repair?' — exactly the user-facing scenario "but
I told you in the other diag we already did this, you forgot!".

Storage mirrors `field_reports.py`: JSON-first to disk under
`memory/{slug}/conversation_log/{stamp}_{repair_id}_{conv_id}.md`, plus a
flag-gated mirror to the device's MA store at `/conversation_log/{...}.md`
so the agent can `glob` / `grep` it on the FUSE mount across all past
repairs on the same device.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from anthropic import AsyncAnthropic

from api.agent.memory_stores import ensure_memory_store, upsert_memory
from api.config import get_settings

logger = logging.getLogger("wrench_board.agent.conversation_log")

OUTCOME_VALUES: tuple[str, ...] = ("resolved", "unresolved", "paused", "escalated")
Outcome = Literal["resolved", "unresolved", "paused", "escalated"]


@dataclass
class TestedTarget:
    """One probe / inspection step the tech performed during the session."""

    target: str          # 'rail:PP3V0', 'comp:U1501', 'pin:U7:12'
    result: str          # 'normal', 'dead', 'shorted', 'open', 'hot', 'noisy', …


@dataclass
class HypothesisTrace:
    """One refdes the agent considered as a suspect, with verdict."""

    refdes: str
    verdict: Literal["confirmed", "rejected", "inconclusive"]
    evidence: str = ""   # one short sentence


@dataclass
class SessionLog:
    """Narrative summary of one chat-conversation, scoped per (repair, conv)."""

    log_id: str
    device_slug: str
    repair_id: str
    conv_id: str
    symptom: str
    outcome: Outcome
    tested: list[TestedTarget] = field(default_factory=list)
    hypotheses: list[HypothesisTrace] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)   # field_report ids
    next_steps: str | None = None
    lesson: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_markdown(self) -> str:
        lines = [
            "---",
            f"log_id: {self.log_id}",
            f"device_slug: {self.device_slug}",
            f"repair_id: {self.repair_id}",
            f"conv_id: {self.conv_id}",
            f"outcome: {self.outcome}",
            f"symptom: {json.dumps(self.symptom, ensure_ascii=False)}",
            f"created_at: {self.created_at}",
            "---",
            "",
            f"# {self.outcome.upper()} — {self.symptom}",
            "",
            f"**Repair:** `{self.repair_id}` · **Conversation:** `{self.conv_id}`",
            "",
        ]
        if self.tested:
            lines.append("## Symptoms tested")
            lines.append("")
            for t in self.tested:
                lines.append(f"- `{t.target}` → {t.result}")
            lines.append("")
        if self.hypotheses:
            lines.append("## Hypotheses explored")
            lines.append("")
            for h in self.hypotheses:
                evid = f" — {h.evidence}" if h.evidence else ""
                lines.append(f"- `{h.refdes}` · **{h.verdict}**{evid}")
            lines.append("")
        if self.findings:
            lines.append("## Archived findings (field_reports)")
            lines.append("")
            for fid in self.findings:
                lines.append(f"- `{fid}`")
            lines.append("")
        if self.next_steps:
            lines.append("## Next steps")
            lines.append("")
            lines.append(self.next_steps)
            lines.append("")
        if self.lesson:
            lines.append("## Lesson")
            lines.append("")
            lines.append(self.lesson)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
_YAML_LINE_RE = re.compile(r"^(\w+):\s*(.*)$")


def _parse_log(path: Path) -> SessionLog | None:
    """Best-effort parse of a saved log file. Returns None on malformed input.

    Frontmatter is the source of truth — body is human-readable Markdown that
    we don't try to re-parse here (the structured form is the original tool
    payload, which we don't need to round-trip).
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
        outcome = meta["outcome"]
        if outcome not in OUTCOME_VALUES:
            return None
        return SessionLog(
            log_id=meta["log_id"],
            device_slug=meta["device_slug"],
            repair_id=meta["repair_id"],
            conv_id=meta["conv_id"],
            symptom=meta["symptom"],
            outcome=outcome,  # type: ignore[arg-type]
            created_at=meta.get("created_at") or datetime.now(UTC).isoformat(),
        )
    except KeyError:
        return None


def _logs_dir(device_slug: str, memory_root: Path) -> Path:
    return memory_root / device_slug / "conversation_log"


def _slug(text: str, max_len: int = 40) -> str:
    frag = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip()).strip("-")
    return (frag or "unknown")[:max_len]


async def record_session_log(
    *,
    client: AsyncAnthropic | None,
    device_slug: str,
    repair_id: str,
    conv_id: str,
    symptom: str,
    outcome: str,
    tested: list[dict[str, str]] | None = None,
    hypotheses: list[dict[str, str]] | None = None,
    findings: list[str] | None = None,
    next_steps: str | None = None,
    lesson: str | None = None,
    memory_root: Path | None = None,
) -> dict[str, Any]:
    """Append (idempotent overwrite per conv_id) one session log.

    JSON-first; MA mirror when the flag is on. Returns a status dict.
    Never raises — MA mirror failure leaves the JSON record intact.
    """
    if outcome not in OUTCOME_VALUES:
        return {
            "ok": False,
            "error": f"outcome must be one of {OUTCOME_VALUES}, got {outcome!r}",
        }

    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)

    created_at = datetime.now(UTC)
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    # log_id = stamp + repair + conv. Filename is the same, so a re-call on
    # the same (repair, conv) overwrites cleanly (path-based dedup, no glob).
    log_id = f"{stamp}_{_slug(repair_id, 24)}_{_slug(conv_id, 24)}"

    log = SessionLog(
        log_id=log_id,
        device_slug=device_slug,
        repair_id=repair_id,
        conv_id=conv_id,
        symptom=symptom,
        outcome=outcome,  # type: ignore[arg-type]
        tested=[TestedTarget(**t) for t in (tested or [])],
        hypotheses=[HypothesisTrace(**h) for h in (hypotheses or [])],
        findings=list(findings or []),
        next_steps=next_steps,
        lesson=lesson,
        created_at=created_at.isoformat(),
    )
    markdown = log.to_markdown()

    logs_dir = _logs_dir(device_slug, memory_root)
    logs_dir.mkdir(parents=True, exist_ok=True)
    # Per-conv filename (NOT per-call) — same conv_id rewrites in place.
    conv_filename = f"{_slug(repair_id, 24)}_{_slug(conv_id, 24)}.md"
    file_path = logs_dir / conv_filename
    file_path.write_text(markdown, encoding="utf-8")
    logger.info(
        "[SessionLog] Wrote slug=%s repair=%s conv=%s outcome=%s",
        device_slug, repair_id, conv_id, outcome,
    )

    status: dict[str, Any] = {
        "ok": True,
        "log_id": log_id,
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
        conv_filename=conv_filename,
        markdown=markdown,
    )
    return status


async def _mirror_to_managed_agents(
    *,
    client: AsyncAnthropic,
    device_slug: str,
    conv_filename: str,
    markdown: str,
) -> str:
    store_id = await ensure_memory_store(client, device_slug)
    if store_id is None:
        return "skipped:no_store"

    result = await upsert_memory(
        client,
        store_id=store_id,
        path=f"/conversation_log/{conv_filename}",
        content=markdown,
    )
    if result is None:
        logger.warning(
            "[SessionLog] MA mirror failed for slug=%s file=%s",
            device_slug, conv_filename,
        )
        return "error:upsert_failed"
    return "mirrored"


def list_session_logs(
    *,
    device_slug: str,
    memory_root: Path | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return logs sorted newest-first. Pure disk read."""
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    logs_dir = _logs_dir(device_slug, memory_root)
    if not logs_dir.exists():
        return []

    logs: list[SessionLog] = []
    for path in logs_dir.glob("*.md"):
        log = _parse_log(path)
        if log is None:
            logger.warning("[SessionLog] Skipping malformed log: %s", path)
            continue
        logs.append(log)

    logs.sort(key=lambda lg: lg.created_at, reverse=True)
    logs = logs[: max(limit, 0)]
    return [
        {
            "log_id": lg.log_id,
            "device_slug": lg.device_slug,
            "repair_id": lg.repair_id,
            "conv_id": lg.conv_id,
            "symptom": lg.symptom,
            "outcome": lg.outcome,
            "created_at": lg.created_at,
        }
        for lg in logs
    ]
