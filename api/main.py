"""FastAPI application entrypoint for microsolder-agent."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api import __version__
from api.board.router import router as board_router
from api.config import get_settings
from api.logging_setup import configure_logging
from api.pipeline import router as pipeline_router
from api.profile.router import router as profile_router

logger = logging.getLogger("microsolder.main")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("microsolder-agent v%s starting up", __version__)
    logger.info(
        "main model=%s fast model=%s", settings.anthropic_model_main, settings.anthropic_model_fast
    )
    yield
    logger.info("microsolder-agent shutting down")


app = FastAPI(
    title="microsolder-agent",
    version=__version__,
    description="Agent-native board-level diagnostics workbench.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pipeline_router)
app.include_router(board_router)
app.include_router(profile_router)


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness probe."""
    return JSONResponse({"status": "ok", "version": __version__})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Legacy echo endpoint — kept so old smoke tests keep passing.

    The real diagnostic loop lives at `/ws/diagnostic/{device_slug}`.
    """
    await websocket.accept()
    logger.info("WebSocket connection opened")
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"text": raw}

            user_text = payload.get("text", "")
            reply = {
                "type": "message",
                "role": "assistant",
                "text": f"(not implemented yet) received: {user_text}",
            }
            await websocket.send_text(json.dumps(reply))
    except WebSocketDisconnect:
        logger.info("WebSocket connection closed")


_VALID_TIERS = {"fast", "normal", "deep"}


@app.websocket("/ws/diagnostic/{device_slug}")
async def diagnostic_session(websocket: WebSocket, device_slug: str) -> None:
    """Diagnostic conversation. `DIAGNOSTIC_MODE` env var picks the runtime.

    - `managed` (default): Anthropic Managed Agents persistent session +
      custom-tool dispatch. Requires a prior `bootstrap_managed_agent.py` run.
    - `direct`: plain `messages.create` tool-use loop. No bootstrap needed;
      used when the Managed Agents beta is unavailable.

    Query param `tier` selects the model: `fast` (Haiku), `normal` (Sonnet),
    `deep` (Opus). Defaults to `fast` for cheap dev traffic. Changing tier in
    the frontend reconnects the WS — it's an explicit new conversation.
    """
    tier = websocket.query_params.get("tier", "fast").lower()
    if tier not in _VALID_TIERS:
        tier = "fast"
    # Optional: scope the session to a specific repair_id. When set, the
    # backend loads past messages from memory/{slug}/repairs/{repair_id}/
    # messages.jsonl and replays them; every new turn appends. Without it,
    # each WS open starts a fresh (unpersisted) conversation.
    repair_id = websocket.query_params.get("repair") or None

    mode = os.environ.get("DIAGNOSTIC_MODE", "managed").lower()
    if mode == "direct":
        from api.agent.runtime_direct import run_diagnostic_session_direct

        await run_diagnostic_session_direct(
            websocket, device_slug, tier=tier, repair_id=repair_id
        )
    else:
        from api.agent.runtime_managed import run_diagnostic_session_managed

        await run_diagnostic_session_managed(
            websocket, device_slug, tier=tier, repair_id=repair_id  # type: ignore[arg-type]
        )


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:
    logger.warning("web/ directory not found at %s — static files not mounted", WEB_DIR)
