# SPDX-License-Identifier: Apache-2.0
"""FastAPI application entrypoint for wrench-board."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api import __version__
from api.agent.macros import macro_path_for
from api.board.router import router as board_router
from api.config import get_settings
from api.logging_setup import configure_logging
from api.pipeline import router as pipeline_router
from api.profile.router import router as profile_router
from api.ws_security import enforce_ws_origin

logger = logging.getLogger("wrench_board.main")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("wrench-board v%s starting up", __version__)
    logger.info(
        "main model=%s fast model=%s", settings.anthropic_model_main, settings.anthropic_model_fast
    )
    if not settings.anthropic_api_key:
        logger.warning(
            "ANTHROPIC_API_KEY is empty — pipeline + diagnostic WS will reject "
            "every request until it's set in .env. Pure-data endpoints "
            "(/health, /pipeline/packs read, board parsing) keep working."
        )
    yield
    logger.info("wrench-board shutting down")


app = FastAPI(
    title="wrench-board",
    version=__version__,
    description="Agent-native board-level diagnostics workbench.",
    lifespan=lifespan,
)

# CORS: drop "*" + credentials (browsers reject that combo anyway) in favor
# of an explicit allowlist from settings. Default list covers local dev; set
# CORS_ALLOW_ORIGINS in .env to widen.
_cors_raw = get_settings().cors_allow_origins
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
_cors_wildcard = _cors_origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=not _cors_wildcard,
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


_MACRO_MIME = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
}


@app.get("/api/macros/{slug}/{repair_id}/{filename}")
async def get_macro(slug: str, repair_id: str, filename: str) -> FileResponse:
    """Serve a stored macro image for chat replay rendering.

    Both Flow A (tech upload) and Flow B (agent cam_capture) write under
    `memory/{slug}/repairs/{repair_id}/macros/`. This route resolves
    `image_ref.path` references stored in `messages.jsonl` so the frontend
    can re-render image bubbles when the chat history reloads.

    Path validation delegates to `api.agent.macros.macro_path_for` which
    blocks traversal (`..`, `/`, leading dot, escape via resolve()).
    """
    settings = get_settings()
    try:
        path = macro_path_for(
            memory_root=Path(settings.memory_root),
            slug=slug, repair_id=repair_id, filename=filename,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="macro not found")
    media_type = _MACRO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


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

    Origin check runs first: the CORS middleware doesn't cover the WS
    handshake, so without this guard any cross-origin browser page could
    open a session and inject `message` frames. See ``api.ws_security``.
    """
    if not await enforce_ws_origin(websocket):
        return

    tier = websocket.query_params.get("tier", "fast").lower()
    if tier not in _VALID_TIERS:
        tier = "fast"
    # Optional: scope the session to a specific repair_id. When set, the
    # backend loads past messages from memory/{slug}/repairs/{repair_id}/
    # messages.jsonl and replays them; every new turn appends. Without it,
    # each WS open starts a fresh (unpersisted) conversation.
    repair_id = websocket.query_params.get("repair") or None
    # Optional: target a specific conversation within the repair. None = use
    # the most recent (or migrate a legacy flat messages.jsonl on first open).
    # "new" = always create a fresh conversation. Any other value must match
    # an existing conversation id, otherwise ensure_conversation raises.
    conv_id = websocket.query_params.get("conv") or None

    mode = os.environ.get("DIAGNOSTIC_MODE", "managed").lower()
    if mode == "direct":
        from api.agent.runtime_direct import run_diagnostic_session_direct

        await run_diagnostic_session_direct(
            websocket, device_slug, tier=tier, repair_id=repair_id, conv_id=conv_id
        )
    else:
        from api.agent.runtime_managed import run_diagnostic_session_managed

        await run_diagnostic_session_managed(
            websocket, device_slug, tier=tier, repair_id=repair_id, conv_id=conv_id  # type: ignore[arg-type]
        )


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles subclass that disables browser caching for every served file.

    Why: the diagnostic chat panel is loaded as a tree of ES modules
    (`js/main.js` → `js/llm.js` → `js/protocol.js` → …). Browsers cache
    each module URL aggressively and ES module imports are NOT invalidated
    by bumping the parent script's `?v=` query string — the relative
    `import './foo.js'` resolves to the bare URL. In dev that means edits
    to a sibling module silently no-op until the tech remembers to
    Ctrl+Shift+R, and stale cached versions keep dropping unhandled WS
    events through old code paths (the recurring `?{...}` raw-JSON dumps
    in chat the user kept seeing). No-store is heavy-handed for a prod
    CDN but exactly right for a local FastAPI dev server: every reload
    pulls fresh code with no stale-module footguns.
    """

    async def get_response(self, path, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


if WEB_DIR.is_dir():
    app.mount("/", _NoCacheStaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:
    logger.warning("web/ directory not found at %s — static files not mounted", WEB_DIR)
