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
from api.agent.board_ref import set_board_ref
from api.agent.macros import macro_path_for
from api.board.router import router as board_router
from api.config import get_settings
from api.http_security import (
    ServiceTokenMiddleware,
    should_fail_unprotected,
    should_warn_unprotected,
)
from api.logging_setup import configure_logging
from api.pipeline import router as pipeline_router
from api.profile.router import router as profile_router
from api.stock import stock_router
from api.ws_security import enforce_ws_origin, enforce_ws_service_token

logger = logging.getLogger("wrench_board.main")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


async def _prewarm_active_boardviews(memory_root: Path) -> None:
    """Parse every device's active boardview into the /render cache.

    Boardview parsing is the heaviest sync work the API does (a 5 MB .tvw
    is ~5 s of CPU); without warming, the first dashboard open for each
    device pays that 5 s up front. Runs in a background task so the
    server is answering requests during the warm — each parse is offloaded
    to a worker thread so the event loop stays responsive.

    Resolution mirrors `_find_boardview`: `active_sources.json` pin first,
    then `board_assets/{slug}.<ext>`, then any `uploads/*-boardview-*`.
    """
    import asyncio  # local — keep startup imports cheap

    from api.board.parser.base import parser_for
    from api.board.render import to_render_payload
    from api.board.router import _RENDER_CACHE_MAX_ENTRIES, _render_cache
    from api.pipeline.routes.packs import _find_boardview

    if not memory_root.exists():
        return

    parsed = 0
    failed = 0
    for pack_dir in sorted(memory_root.iterdir()):
        if not pack_dir.is_dir() or pack_dir.name.startswith("_"):
            continue
        if len(_render_cache) >= _RENDER_CACHE_MAX_ENTRIES:
            logger.info("[prewarm] cache full, stopping after %d", parsed)
            break
        slug = pack_dir.name
        path = _find_boardview(slug, pack_dir)
        if path is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cache_key = (str(path), mtime)
        if cache_key in _render_cache:
            continue
        try:
            board = await asyncio.to_thread(parser_for(path).parse_file, path)
            payload = await asyncio.to_thread(to_render_payload, board)
            _render_cache[cache_key] = payload
            parsed += 1
            logger.info("[prewarm] %s cached (%s)", slug, path.name)
        except Exception:  # noqa: BLE001 — fire-and-forget; any failure logs + skips
            failed += 1
            logger.warning("[prewarm] failed for %s (%s)", slug, path.name, exc_info=True)
    logger.info("[prewarm] done: %d cached, %d failed", parsed, failed)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks."""
    import asyncio  # local — only needed for the prewarm task

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
    # Fail-fast : en contexte EXPLICITEMENT production (ENV=production) SANS
    # service-token, le moteur serait ouvert à tout Internet → on REFUSE de
    # démarrer (symétrie avec le fail-fast cloud). Le self-host (ENV non-prod,
    # même en docker 0.0.0.0) n'est pas touché.
    if should_fail_unprotected(
        token=settings.engine_service_token,
        env=os.getenv("ENV", ""),
    ):
        raise RuntimeError(
            "ENGINE_SERVICE_TOKEN manquant en production — le moteur REFUSE de "
            "démarrer (il serait accessible sans auth depuis Internet). Définis "
            "ENGINE_SERVICE_TOKEN (identique à celui du cloud) et garde le moteur "
            "sur un réseau privé. Cf. DEPLOYMENT.md."
        )
    # Filet plus souple : WARN si prod-like par heuristique (bind 0.0.0.0) SANS
    # token — ne crash pas (le self-host légitime en docker reste silencieux).
    if should_warn_unprotected(
        token=settings.engine_service_token,
        host=os.getenv("HOST", "127.0.0.1"),
        env=os.getenv("ENV", ""),
    ):
        logger.warning(
            "ENGINE_SERVICE_TOKEN vide en contexte prod-like — le moteur est OUVERT "
            "(toutes les routes accessibles sans auth). En managé : set le token + "
            "réseau privé (cf. DEPLOYMENT.md)."
        )
    # Seed shipped demo packs (e.g. MNT Reform) so the first-run example tour
    # has a fully-analyzed device to walk. Idempotent + non-destructive.
    try:
        from api.pipeline.demo_seed import seed_demo_packs

        seeded = seed_demo_packs(Path(settings.memory_root))
        if seeded:
            logger.info("demo packs seeded: %d", seeded)
    except Exception as exc:  # noqa: BLE001 — seeding must never block startup
        logger.warning("demo-pack seeding skipped: %s", exc)
    # Kick off boardview pre-warm in background — don't await. The server
    # is answering requests immediately; per-thread parse populates the
    # /render cache so the first dashboard open for each device is instant.
    asyncio.create_task(_prewarm_active_boardviews(Path(settings.memory_root)))
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

# Gate HTTP du service-token : exige le bearer en mode managé, no-op en
# self-host (token vide). Ajouté APRÈS CORS → plus externe → s'exécute avant
# la logique applicative ; laisse passer OPTIONS (préflight) + /health.
app.add_middleware(
    ServiceTokenMiddleware,
    expected_token=get_settings().engine_service_token,
)

app.include_router(pipeline_router)
app.include_router(board_router)
app.include_router(profile_router)
app.include_router(stock_router)


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
    `deep` (Opus). Defaults to `deep` so demo traffic lands on Opus 4.8
    without an explicit tier pick. Changing tier in the frontend reconnects
    the WS — it's an explicit new conversation.

    Origin check runs first: the CORS middleware doesn't cover the WS
    handshake, so without this guard any cross-origin browser page could
    open a session and inject `message` frames. The service-token check runs
    next: when the engine is deployed behind wrenchboard-cloud, only the cloud
    relay (which carries the shared `Authorization: Bearer` token) may open a
    session — a direct websocat against the engine URL is refused, so it can't
    bypass the cloud's auth + quota and burn credits. Both are no-ops in the
    standalone workbench (no allowlist / no token configured). See
    ``api.ws_security``.
    """
    if not await enforce_ws_origin(websocket):
        return
    if not await enforce_ws_service_token(websocket):
        return

    tier = websocket.query_params.get("tier", "deep").lower()
    if tier not in _VALID_TIERS:
        tier = "deep"
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
    # Multi-tenant: the cloud front-door injects X-Owner-Ref (the tenant id) on
    # the handshake so the session's owner-sensitive tools (stock) write to the
    # right tenant's private store. Absent in standalone/self-host.
    owner_ref = websocket.headers.get("X-Owner-Ref") or None
    # Plan capability injected by the cloud (the only gatekeeper): may this
    # session's tenant trigger a paid pack enrichment (mb_expand_knowledge)?
    # Header absent → standalone/self-host → True (unrestricted). The cloud
    # always sends an explicit "true"/"false"; only "false" disables it.
    can_expand = (websocket.headers.get("X-Wb-Can-Expand") or "true").strip().lower() != "false"
    # Optional board number (PCB revision, e.g. "820-02016") supplied by the
    # client as a query param. Absent → None → no board-delta injection. No
    # trust logic here: the public engine carries this as an opaque key only.
    set_board_ref(websocket.query_params.get("board"))

    mode = os.environ.get("DIAGNOSTIC_MODE", "managed").lower()
    if mode == "direct":
        from api.agent.runtime_direct import run_diagnostic_session_direct

        await run_diagnostic_session_direct(
            websocket, device_slug, tier=tier, repair_id=repair_id, conv_id=conv_id,
            owner_ref=owner_ref, can_expand=can_expand,
        )
    else:
        from api.agent.runtime_managed import run_diagnostic_session_managed

        await run_diagnostic_session_managed(
            websocket, device_slug, tier=tier, repair_id=repair_id, conv_id=conv_id,  # type: ignore[arg-type]
            owner_ref=owner_ref, can_expand=can_expand,
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
