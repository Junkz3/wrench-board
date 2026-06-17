"""POST /pipeline/packs/{slug}/confirm-kind — Task 9.

Records the technician's resolved device kind, clears the pending-confirmation
marker, and re-runs the pipeline with `confirmed_device_kind`.

Uses the shared `memory_root` fixture (tests/conftest.py) which seeds the real
`MEMORY_ROOT` env var and resets the cached Settings singleton — the engine
reads `MEMORY_ROOT` (no `WRENCH_BOARD_` prefix, no env_prefix in config.py).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.main import app


def test_confirm_kind_clears_pending_and_reruns(memory_root):
    pack = memory_root / "dev-x"
    pack.mkdir(parents=True)
    (pack / "pending_kind.json").write_text(
        json.dumps(
            {
                "user_declared": "laptop_logic_board",
                "graph_inferred": "gpu_card",
                "status": "needs_confirmation",
                "resolved_kind": None,
                "confidence": 0.9,
                "evidence": "e",
            }
        )
    )
    # The endpoint now launches the rerun via the repairs.py wrapper
    # (`_run_pipeline_with_events`), which calls `generate_knowledge_pack`
    # through the `api.pipeline` package (`_pkg.generate_knowledge_pack`). Patch
    # that seam so the background task records the threaded kwargs without
    # spending tokens. The await happens on a loop tick, so we let the task run
    # to completion below before asserting on call_args.
    with patch("api.pipeline.generate_knowledge_pack", new=AsyncMock()) as gen:
        with TestClient(app) as c:
            r = c.post("/pipeline/packs/dev-x/confirm-kind", json={"device_kind": "gpu_card"})

    assert r.status_code == 200
    body = r.json()
    assert body["device_slug"] == "dev-x"
    assert body["confirmed_kind"] == "gpu_card"
    assert body["status"] == "rebuilding"
    # Pending marker cleared.
    assert not (pack / "pending_kind.json").is_file()
    # Confirmed kind threaded into the rerun (the wrapper forwards it to
    # generate_knowledge_pack). TestClient runs the app on its own event loop
    # and tears it down on __exit__, so the background task has run by here.
    gen.assert_awaited_once()
    assert gen.await_args.kwargs["confirmed_device_kind"] == "gpu_card"


def test_confirm_kind_404_when_pack_missing(memory_root):
    # Valid-format slug but no directory on disk → 404 before any rerun.
    with TestClient(app) as c:
        r = c.post("/pipeline/packs/never-built/confirm-kind", json={"device_kind": "gpu_card"})
    assert r.status_code == 404


def test_confirm_kind_rejects_unknown_enum(memory_root):
    # Pydantic body validation precedes the path logic, so a bad enum is 422
    # even though the dir exists (the 404 guard is never reached).
    (memory_root / "dev-x").mkdir(parents=True)
    with TestClient(app) as c:
        r = c.post("/pipeline/packs/dev-x/confirm-kind", json={"device_kind": "toaster"})
    assert r.status_code == 422
