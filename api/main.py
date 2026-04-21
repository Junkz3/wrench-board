"""FastAPI application entrypoint for microsolder-agent."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api import __version__
from api.config import get_settings
from api.logging_setup import configure_logging
from api.pipeline import router as pipeline_router

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


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness probe."""
    return JSONResponse({"status": "ok", "version": __version__})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Placeholder WebSocket — echoes back with a `not implemented yet` note.

    The real agent loop will live here once `api/agent/` is implemented.
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


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:
    logger.warning("web/ directory not found at %s — static files not mounted", WEB_DIR)
