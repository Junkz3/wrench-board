"""Synchronous full-pipeline endpoint — `POST /pipeline/generate`.

Blocks for ~30–120 s while Scout → Registry → Writers → Auditor runs.
Background-task variants live in `repairs.py` (the WS-relayed version
fired by `POST /repairs`).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from api.pipeline.models import GenerateRequest
from api.pipeline.orchestrator import generate_knowledge_pack
from api.pipeline.schemas import PipelineResult

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


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
