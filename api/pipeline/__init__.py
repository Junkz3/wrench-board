"""Pipeline package — FastAPI router for the knowledge-generation factory.

Exposes:
    POST /pipeline/generate  — run the full pipeline for one device (blocking).
    GET  /pipeline/packs     — list packs on disk.
    GET  /pipeline/packs/{device_slug}  — return metadata for a generated pack.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.config import get_settings
from api.pipeline.orchestrator import _slugify, generate_knowledge_pack
from api.pipeline.schemas import PipelineResult

logger = logging.getLogger("microsolder.pipeline.api")

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class GenerateRequest(BaseModel):
    device_label: str = Field(
        min_length=2,
        max_length=200,
        description="Human-readable device identifier (e.g. 'Raspberry Pi 4 Model B').",
    )


@router.post("/generate", response_model=PipelineResult)
async def generate(request: GenerateRequest) -> PipelineResult:
    """Run the full pipeline synchronously and return the result on completion.

    Expect this call to block for ~30–120 seconds depending on Scout web_search usage
    and whether the Auditor triggers revise rounds.
    """
    logger.info("[API] /pipeline/generate · device=%r", request.device_label)
    try:
        return await generate_knowledge_pack(request.device_label)
    except RuntimeError as exc:
        logger.exception("[API] Pipeline failed for device=%r", request.device_label)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class PackSummary(BaseModel):
    device_slug: str
    disk_path: str
    has_raw_dump: bool
    has_registry: bool
    has_knowledge_graph: bool
    has_rules: bool
    has_dictionary: bool
    has_audit_verdict: bool


def _summarize_pack(pack_dir: Path) -> PackSummary:
    return PackSummary(
        device_slug=pack_dir.name,
        disk_path=str(pack_dir),
        has_raw_dump=(pack_dir / "raw_research_dump.md").exists(),
        has_registry=(pack_dir / "registry.json").exists(),
        has_knowledge_graph=(pack_dir / "knowledge_graph.json").exists(),
        has_rules=(pack_dir / "rules.json").exists(),
        has_dictionary=(pack_dir / "dictionary.json").exists(),
        has_audit_verdict=(pack_dir / "audit_verdict.json").exists(),
    )


@router.get("/packs", response_model=list[PackSummary])
async def list_packs() -> list[PackSummary]:
    settings = get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        return []
    return sorted(
        (_summarize_pack(d) for d in root.iterdir() if d.is_dir()),
        key=lambda s: s.device_slug,
    )


@router.get("/packs/{device_slug}", response_model=PackSummary)
async def get_pack(device_slug: str) -> PackSummary:
    settings = get_settings()
    root = Path(settings.memory_root)
    # Normalize: accept either a raw slug or a device_label.
    slug = _slugify(device_slug)
    pack_dir = root / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    return _summarize_pack(pack_dir)


__all__ = ["router"]
