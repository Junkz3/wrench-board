"""WebSocket pipeline-progress relay — `WS /pipeline/progress/{slug}`.

Subscribes the client to the per-slug event bus and forwards every
event verbatim until disconnect. Origin check then service-token check
run first (see `api.ws_security`) so cross-origin browser tabs can't
silently subscribe AND direct access to the engine URL is blocked in
managed deployment.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.pipeline import events
from api.pipeline.orchestrator import _slugify
from api.ws_security import enforce_ws_origin, enforce_ws_service_token

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


@router.websocket("/progress/{device_slug}")
async def progress_ws(websocket: WebSocket, device_slug: str) -> None:
    """Stream pipeline events for this slug until the client disconnects.

    Emits a `{type:"subscribed", device_slug}` ack as soon as the subscription
    is live, so the client knows it won't miss subsequent events. Terminal
    events (pipeline_finished / pipeline_failed) are still delivered normally;
    it's up to the client to close the socket when it's done consuming.

    Origin check runs first to keep cross-origin browser pages from
    silently subscribing to another technician's pipeline progress
    stream. Service-token check runs next: when the engine is deployed
    behind wrenchboard-cloud, only the cloud relay (which carries
    ``Authorization: Bearer <token>``) may subscribe — direct ``websocat``
    access to the engine URL is refused. Both are no-ops in the standalone
    workbench (no allowlist / no token configured). See ``api.ws_security``.
    """
    if not await enforce_ws_origin(websocket):
        return
    if not await enforce_ws_service_token(websocket):
        return

    slug = _slugify(device_slug)
    await websocket.accept()
    queue = events.subscribe(slug)
    try:
        await websocket.send_text(json.dumps({"type": "subscribed", "device_slug": slug}))
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event))
    except WebSocketDisconnect:
        logger.info("[API] /pipeline/progress/%s · client disconnected", slug)
    finally:
        events.unsubscribe(slug, queue)
