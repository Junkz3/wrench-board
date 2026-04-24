"""Phase 1 — Scout. Autonomous web research using the native Claude web_search tool.

Output: a single Markdown document (the "raw research dump"). No JSON, no structured form.

The Scout runs once; if the produced dump falls below the configured thresholds
(min symptoms / components / sources) the orchestrator re-invokes it with a
broader-search suffix. After `max_retries` failures we raise `ThinScoutDumpError`
so the pipeline stops instead of paying for downstream phases on a bankrupt dump.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from api.pipeline.prompts import SCOUT_RETRY_SUFFIX, SCOUT_SYSTEM, SCOUT_USER_TEMPLATE

if TYPE_CHECKING:
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("microsolder.pipeline.scout")


class ThinScoutDumpError(RuntimeError):
    """Raised when the Scout dump fails the threshold check after all retries."""


@dataclass(frozen=True)
class DumpAssessment:
    symptoms: int
    components: int
    sources: int
    viable: bool

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "symptoms": self.symptoms,
            "components": self.components,
            "sources": self.sources,
            "viable": self.viable,
        }


_SYMPTOM_RE = re.compile(r"^\s*-\s+\*\*Symptom:\*\*", re.MULTILINE)
_URL_RE = re.compile(r"https?://[^\s)\]\"']+")
_COMPONENT_LINE_RE = re.compile(r"^\s*-\s+\*\*([^*]+?)\*\*", re.MULTILINE)
_COMPONENTS_SECTION_RE = re.compile(
    r"##\s+Components mentioned.*?(?=\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def assess_dump(
    dump: str,
    *,
    min_symptoms: int,
    min_components: int,
    min_sources: int,
) -> DumpAssessment:
    """Count the load-bearing entities in a Scout dump.

    - symptoms: number of '**Symptom:**' blocks
    - components: number of distinct '- **<name>**' lines inside the
      '## Components mentioned by the community' section
    - sources: number of unique URLs anywhere in the dump
    """
    symptoms = len(_SYMPTOM_RE.findall(dump))

    section = _COMPONENTS_SECTION_RE.search(dump)
    if section:
        names = {m.group(1).strip() for m in _COMPONENT_LINE_RE.finditer(section.group(0))}
        components = len(names)
    else:
        components = 0

    sources = len({url.rstrip(".,;:") for url in _URL_RE.findall(dump)})

    viable = (
        symptoms >= min_symptoms and components >= min_components and sources >= min_sources
    )
    return DumpAssessment(
        symptoms=symptoms, components=components, sources=sources, viable=viable
    )


async def run_scout(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    max_continuations: int = 3,
    min_symptoms: int = 3,
    min_components: int = 3,
    min_sources: int = 3,
    max_retries: int = 1,
    stats: PhaseTokenStats | None = None,
) -> str:
    """Execute Phase 1 — return the raw research Markdown dump.

    Re-runs Scout up to `max_retries` times if the dump fails the threshold check.
    Each retry widens the search scope via `SCOUT_RETRY_SUFFIX`. After all
    retries, raises `ThinScoutDumpError` — the orchestrator must surface that
    instead of burning cash on Phases 2-4 with a bankrupt dump.
    """
    logger.info("[Scout] Starting research for device=%r", device_label)

    last_dump: str | None = None
    last_assessment: DumpAssessment | None = None

    for attempt in range(max_retries + 1):
        dump = await _scout_once(
            client=client,
            model=model,
            device_label=device_label,
            max_continuations=max_continuations,
            attempt=attempt,
            stats=stats,
        )
        last_dump = dump
        last_assessment = assess_dump(
            dump,
            min_symptoms=min_symptoms,
            min_components=min_components,
            min_sources=min_sources,
        )
        logger.info(
            "[Scout] Attempt %d assessment: %s",
            attempt + 1,
            last_assessment.as_dict(),
        )
        if last_assessment.viable:
            return dump

        logger.warning(
            "[Scout] Dump below thresholds (min sym=%d comp=%d src=%d) · "
            "attempt %d/%d",
            min_symptoms,
            min_components,
            min_sources,
            attempt + 1,
            max_retries + 1,
        )

    assert last_dump is not None and last_assessment is not None
    raise ThinScoutDumpError(
        f"Scout dump too thin after {max_retries + 1} attempts: "
        f"{last_assessment.as_dict()} (thresholds: "
        f"symptoms>={min_symptoms}, components>={min_components}, "
        f"sources>={min_sources})"
    )


async def _scout_once(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    max_continuations: int,
    attempt: int,
    stats: PhaseTokenStats | None = None,
) -> str:
    """One end-to-end Scout run, including server-side `pause_turn` handling."""
    user_prompt = SCOUT_USER_TEMPLATE.format(device_label=device_label)
    if attempt > 0:
        user_prompt = user_prompt + SCOUT_RETRY_SUFFIX

    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    web_search_tool = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 12,
    }

    total_input = 0
    total_output = 0

    for iteration in range(max_continuations + 1):
        logger.info("[Scout] API call iteration=%d (attempt=%d)", iteration + 1, attempt + 1)
        response = await client.messages.create(
            model=model,
            max_tokens=16000,
            system=SCOUT_SYSTEM,
            messages=messages,
            tools=[web_search_tool],
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
        )

        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens
        if stats is not None:
            stats.record(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                cache_write=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            )

        if response.stop_reason == "pause_turn":
            logger.info("[Scout] pause_turn — extending conversation to continue")
            messages = [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": response.content},
            ]
            continue

        if response.stop_reason == "end_turn":
            logger.info(
                "[Scout] Attempt %d research complete · tokens in=%d out=%d",
                attempt + 1,
                total_input,
                total_output,
            )
            break

        # stop_reason == "max_tokens" or "refusal" — surface clearly
        logger.warning("[Scout] Unexpected stop_reason=%r", response.stop_reason)
        break
    else:
        logger.warning(
            "[Scout] Hit max_continuations=%d without natural end_turn", max_continuations
        )

    text_parts = [block.text for block in response.content if block.type == "text"]
    dump = "\n\n".join(t for t in text_parts if t.strip())

    if not dump:
        raise RuntimeError(
            "[Scout] Produced no text output. Response had "
            f"{len(response.content)} content blocks with types "
            f"{[b.type for b in response.content]}"
        )

    logger.info("[Scout] Web search finished · dump_length=%d chars", len(dump))
    return dump
