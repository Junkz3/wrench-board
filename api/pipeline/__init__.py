"""Pipeline package — FastAPI router for the knowledge-generation factory.

The endpoints themselves live under `api/pipeline/routes/`; this module
just composes the sub-routers into the single `router` mounted by
`api.main`. See each `routes/<name>.py` for the concrete handlers.

Endpoint groups:
    routes/generate.py      — POST /pipeline/generate
    routes/packs.py         — pack listings, taxonomy, full payload, expand, graph
    routes/repairs.py       — /repairs/* CRUD + the orchestration POST
    routes/progress.py      — WS /pipeline/progress/{slug}
    routes/documents.py     — uploads, sources, boardview / schematic.pdf serving
    routes/schematic.py     — ingest-schematic + schematic page/graph/simulate routes
    routes/measurements.py  — measurement journal under a repair

Re-exports preserved for backward compatibility:
    `router`                    — mounted by `api.main`
    `_find_boardview`, `_find_owner_boardview` — used by `api.board.router`
    `_run_pipeline_with_events` — used by tests/pipeline/test_pipeline_events
    `generate_knowledge_pack`, `get_settings`,
    `ingest_schematic`, `classify_nets`, `expand_pack`,
    `_maybe_check_coverage`     — used as `patch("api.pipeline.X")` targets in
        the test suite. The route modules look them up dynamically via
        `import api.pipeline as _pkg; _pkg.X(...)` so those patches still
        take effect after the routes/ split.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from api.config import get_settings  # noqa: F401
from api.pipeline import events, sources  # noqa: F401
from api.pipeline.expansion import expand_pack  # noqa: F401
from api.pipeline.orchestrator import generate_knowledge_pack  # noqa: F401
from api.pipeline.routes.board_delta import router as _board_delta_router
from api.pipeline.routes.documents import router as _documents_router
from api.pipeline.routes.generate import router as _generate_router
from api.pipeline.routes.measurements import router as _measurements_router
from api.pipeline.routes.packs import (
    _find_boardview,  # noqa: F401
    _find_owner_boardview,  # noqa: F401
)
from api.pipeline.routes.packs import router as _packs_router
from api.pipeline.routes.progress import router as _progress_router
from api.pipeline.routes.repairs import (
    _maybe_check_coverage,  # noqa: F401
    _run_pipeline_with_events,  # noqa: F401
)
from api.pipeline.routes.repairs import router as _repairs_router
from api.pipeline.routes.schematic import router as _schematic_router
from api.pipeline.schematic.net_classifier import classify_nets  # noqa: F401
from api.pipeline.schematic.orchestrator import ingest_schematic  # noqa: F401

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
router.include_router(_board_delta_router)
router.include_router(_generate_router)
router.include_router(_schematic_router)
router.include_router(_packs_router)
router.include_router(_repairs_router)
router.include_router(_progress_router)
router.include_router(_documents_router)
router.include_router(_measurements_router)

__all__ = ["router"]
