"""Tests for POST /pipeline/repairs + WS /pipeline/progress/{slug}.

The real pipeline calls Anthropic and takes tens of seconds; these tests
patch `generate_knowledge_pack` to an instant stub and publish events
directly on the bus so we can validate wiring without network.
"""

from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api import config as config_mod
from api.main import app
from api.pipeline import events


@pytest.fixture(autouse=True)
def _reset_bus():
    events.reset()
    yield
    events.reset()


async def _fake_pipeline(device_label, **kwargs):
    """Drop-in replacement for generate_knowledge_pack: emits events, returns None."""
    on_event = kwargs.get("on_event")
    if on_event:
        await on_event({"type": "pipeline_started", "device_slug": "demo", "device_label": device_label})
        await on_event({"type": "phase_started", "phase": "scout"})
        await on_event({"type": "phase_finished", "phase": "scout", "elapsed_s": 0.01})
        await on_event({"type": "pipeline_finished", "device_slug": "demo", "status": "APPROVED",
                        "revise_rounds_used": 0, "consistency_score": 1.0})


def test_repairs_endpoint_returns_id_and_slug(memory_root, client):
    with patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)):
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "no 3V3 rail, device won't power on"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["device_slug"] == "demo-pi"
    assert len(body["repair_id"]) > 0
    assert body["pipeline_started"] is True


def test_create_repair_rejects_json_body(memory_root, client):
    # The endpoint is multipart-only now; a JSON body must 422 (not silently accepted).
    r = client.post("/pipeline/repairs", json={"device_label": "Some Board", "symptom": "dead board"})
    assert r.status_code == 422


def test_repairs_endpoint_persists_symptom_file(memory_root, client):
    with patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)):
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "dead PMIC"},
        )
    body = res.json()
    repair_file = memory_root / body["device_slug"] / "repairs" / f"{body['repair_id']}.json"
    assert repair_file.exists()
    data = json.loads(repair_file.read_text())
    assert data["symptom"] == "dead PMIC"
    assert data["device_label"] == "Demo Pi"
    assert data["device_slug"] == "demo-pi"
    assert "created_at" in data


def test_repairs_endpoint_empty_rules_pack_fires_expand(memory_root, client):
    """Pack complete but rules.json is empty → coverage classifier short-
    circuits to covered=False (no LLM call) and create_repair fires an
    expand_pack round rather than skipping. The full pipeline is NOT
    called. The repair record is persisted either way."""
    slug_dir = memory_root / "demo-pi"
    slug_dir.mkdir()
    (slug_dir / "registry.json").write_text('{"schema_version":"1.0","device_label":"Demo Pi","components":[],"signals":[]}')
    (slug_dir / "knowledge_graph.json").write_text('{"schema_version":"1.0","nodes":[],"edges":[]}')
    (slug_dir / "rules.json").write_text('{"schema_version":"1.0","rules":[]}')
    (slug_dir / "dictionary.json").write_text('{"schema_version":"1.0","entries":[]}')

    with patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ) as m_pipeline, patch(
        "api.pipeline.expand_pack",
        new=AsyncMock(return_value={"new_rules_count": 1, "new_components_count": 0, "total_rules_after": 1}),
    ) as m_expand:
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "no 3V3 rail, device won't power on"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["pipeline_started"] is True
    assert body["pipeline_kind"] == "expand"
    assert body["matched_rule_id"] is None
    m_pipeline.assert_not_called()
    # expand_pack was fired in background — allow the task to run.
    # Py3.12: get_event_loop() raises when no loop is set on the main
    # thread (other tests in the suite close their loops, leaving the
    # thread bare); spin up a fresh one to drive the sleep.
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0.05))
    finally:
        loop.close()
    m_expand.assert_called_once()
    # Repair record persisted regardless.
    assert body["repair_id"]
    repair_file = slug_dir / "repairs" / f"{body['repair_id']}.json"
    assert repair_file.exists()
    payload = json.loads(repair_file.read_text())
    assert payload["status"] == "open"
    assert payload["symptom"] == "no 3V3 rail, device won't power on"


def test_list_repairs_returns_all_sessions_across_devices(memory_root, client):
    """GET /pipeline/repairs should aggregate repair files across every pack,
    sorted newest-first. Powers the home library view.
    """
    # Two different devices, three total repairs.
    for slug, repairs in (
        ("iphone-x-logic-board", [
            {"repair_id": "rA1", "symptom": "no backlight", "created_at": "2026-04-20T10:00:00+00:00"},
            {"repair_id": "rA2", "symptom": "not charging",  "created_at": "2026-04-22T15:00:00+00:00"},
        ]),
        ("mnt-reform-motherboard", [
            {"repair_id": "rB1", "symptom": "LPC lockup", "created_at": "2026-04-21T09:00:00+00:00"},
        ]),
    ):
        rdir = memory_root / slug / "repairs"
        rdir.mkdir(parents=True, exist_ok=True)
        for r in repairs:
            (rdir / f"{r['repair_id']}.json").write_text(json.dumps({
                **r,
                "device_slug": slug,
                "device_label": slug.replace("-", " "),
                "status": "open",
            }))

    res = client.get("/pipeline/repairs")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 3
    # Newest first.
    assert [r["repair_id"] for r in body] == ["rA2", "rB1", "rA1"]
    assert all(r["status"] == "open" for r in body)


def test_get_repair_by_id(memory_root, client):
    (memory_root / "demo-pi" / "repairs").mkdir(parents=True)
    (memory_root / "demo-pi" / "repairs" / "r123.json").write_text(json.dumps({
        "repair_id": "r123",
        "device_slug": "demo-pi",
        "device_label": "Demo Pi",
        "symptom": "won't boot",
        "status": "in_progress",
        "created_at": "2026-04-22T12:00:00+00:00",
    }))
    res = client.get("/pipeline/repairs/r123")
    assert res.status_code == 200
    assert res.json()["status"] == "in_progress"
    assert res.json()["symptom"] == "won't boot"

    res_404 = client.get("/pipeline/repairs/does-not-exist")
    assert res_404.status_code == 404


def test_repairs_endpoint_resolves_by_device_slug_when_provided(memory_root, client):
    """When the client sends device_slug directly, the backend uses it — even
    if the device_label slugifies to something different. Protects against
    Registry-rewrite drift (label changes after the pack dir is named).
    """
    # Pack lives under 'iphone-x-logic-board' on disk, but the internal
    # device_label has been rewritten to something that slugifies differently.
    slug_dir = memory_root / "iphone-x-logic-board"
    slug_dir.mkdir()
    for name, body in (
        ("registry.json", '{"schema_version":"1.0","device_label":"Apple iPhone X logic board","components":[],"signals":[]}'),
        ("knowledge_graph.json", '{"schema_version":"1.0","nodes":[],"edges":[]}'),
        ("rules.json", '{"schema_version":"1.0","rules":[]}'),
        ("dictionary.json", '{"schema_version":"1.0","entries":[]}'),
    ):
        (slug_dir / name).write_text(body)

    with patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ) as m_pipeline, patch(
        "api.pipeline.expand_pack",
        new=AsyncMock(return_value={"new_rules_count": 0, "new_components_count": 0, "total_rules_after": 0}),
    ):
        res = client.post(
            "/pipeline/repairs",
            data={
                "device_label": "Apple iPhone X logic board",  # would slugify to apple-iphone-x-logic-board
                "device_slug": "iphone-x-logic-board",         # but this wins
                "symptom": "pack already exists on disk",
            },
        )
    assert res.status_code == 200
    body = res.json()
    assert body["device_slug"] == "iphone-x-logic-board"
    # Pack complete + empty rules → expand fires (not the full pipeline).
    assert body["pipeline_kind"] == "expand"
    m_pipeline.assert_not_called()


def test_repairs_endpoint_force_rebuild_persists_repair_and_fires_pipeline(memory_root, client):
    """force_rebuild=true on an existing pack must run the pipeline AND write a repair file."""
    slug_dir = memory_root / "demo-pi"
    slug_dir.mkdir()
    for name, body in (
        ("registry.json", '{"schema_version":"1.0","device_label":"Demo Pi","components":[],"signals":[]}'),
        ("knowledge_graph.json", '{"schema_version":"1.0","nodes":[],"edges":[]}'),
        ("rules.json", '{"schema_version":"1.0","rules":[]}'),
        ("dictionary.json", '{"schema_version":"1.0","entries":[]}'),
    ):
        (slug_dir / name).write_text(body)

    with patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)):
        res = client.post(
            "/pipeline/repairs",
            data={
                "device_label": "Demo Pi",
                "symptom": "force rebuild even though pack exists",
                "force_rebuild": "true",
            },
        )
    assert res.status_code == 200
    body = res.json()
    assert body["pipeline_started"] is True
    assert body["repair_id"]  # non-empty
    assert (slug_dir / "repairs").exists()
    assert list((slug_dir / "repairs").glob("*.json"))


def test_repairs_branch_full_when_pack_absent(memory_root, client):
    """Pack missing on disk → Branch 1: full pipeline fires with focus_symptom."""
    captured_kwargs: dict = {}

    async def _capture_pipeline(device_label, *, on_event=None, focus_symptom=None, **_):
        captured_kwargs["device_label"] = device_label
        captured_kwargs["focus_symptom"] = focus_symptom

    with patch(
        "api.pipeline.generate_knowledge_pack",
        new=AsyncMock(side_effect=_capture_pipeline),
    ):
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Brand New Device", "symptom": "screen is dark on power-on"},
        )
    # Give the background task a tick to run. Py3.12: see note in
    # test_repairs_endpoint_empty_rules_pack_fires_expand — fresh loop
    # to avoid `RuntimeError: no current event loop` under suite-level
    # state pollution.
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0.05))
    finally:
        loop.close()

    assert res.status_code == 200
    body = res.json()
    assert body["pipeline_started"] is True
    assert body["pipeline_kind"] == "full"
    assert body["matched_rule_id"] is None
    assert captured_kwargs["focus_symptom"] == "screen is dark on power-on"


def test_repairs_branch_expand_when_pack_complete_and_symptom_uncovered(memory_root, client, monkeypatch):
    """Pack complete + coverage classifier says NOT covered → Branch 3: expand."""
    # `_safe_coverage_check` early-returns "no API key" before calling
    # `check_symptom_coverage` when ANTHROPIC_API_KEY is empty, which would
    # bypass the patch below. Inject a fake key + reset settings cache so
    # the wrapper proceeds to the patched call site.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake-for-mocked-call")
    monkeypatch.setattr(config_mod, "_settings", None)
    slug_dir = memory_root / "demo-pi"
    slug_dir.mkdir()
    (slug_dir / "registry.json").write_text('{"schema_version":"1.0","device_label":"Demo Pi","components":[],"signals":[]}')
    (slug_dir / "knowledge_graph.json").write_text('{"schema_version":"1.0","nodes":[],"edges":[]}')
    (slug_dir / "rules.json").write_text(
        '{"schema_version":"1.0","rules":[{"id":"rule-charge-001","symptoms":["no charge"],"likely_causes":[{"refdes":"U1","probability":0.9,"mechanism":"x"}],"confidence":0.8}]}'
    )
    (slug_dir / "dictionary.json").write_text('{"schema_version":"1.0","entries":[]}')

    from api.pipeline.schemas import CoverageCheck

    async def _uncovered(**_kwargs):
        return CoverageCheck(
            covered=False, matched_rule_id=None, confidence=0.2,
            reason="distinct failure mode",
        )

    with patch(
        "api.pipeline.coverage.check_symptom_coverage", new=AsyncMock(side_effect=_uncovered)
    ), patch(
        "api.pipeline.expand_pack",
        new=AsyncMock(return_value={"new_rules_count": 1, "new_components_count": 0, "total_rules_after": 2}),
    ) as m_expand:
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "USB port delivers no 5V"},
        )
    # Py3.12 fresh loop — see note in
    # test_repairs_endpoint_empty_rules_pack_fires_expand.
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0.05))
    finally:
        loop.close()

    body = res.json()
    assert body["pipeline_kind"] == "expand"
    assert body["pipeline_started"] is True
    assert body["matched_rule_id"] is None
    assert body["coverage_reason"] == "distinct failure mode"
    m_expand.assert_called_once()


def test_repairs_branch_expand_threads_owner_ref_to_expand_pack(memory_root, client, monkeypatch):
    """Régression T8 : Branch 3 (pack complet + symptôme non couvert) doit transmettre
    le owner_ref du RepairRequest à expand_pack — sinon l'enrichissement est écrit dans
    promoted/ avec added_by_tenant=None, cassant l'invariant de traçabilité T8."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake-for-mocked-call")
    monkeypatch.setattr(config_mod, "_settings", None)
    slug_dir = memory_root / "demo-pi"
    slug_dir.mkdir()
    (slug_dir / "registry.json").write_text('{"schema_version":"1.0","device_label":"Demo Pi","components":[],"signals":[]}')
    (slug_dir / "knowledge_graph.json").write_text('{"schema_version":"1.0","nodes":[],"edges":[]}')
    (slug_dir / "rules.json").write_text(
        '{"schema_version":"1.0","rules":[{"id":"rule-charge-001","symptoms":["no charge"],"likely_causes":[{"refdes":"U1","probability":0.9,"mechanism":"x"}],"confidence":0.8}]}'
    )
    (slug_dir / "dictionary.json").write_text('{"schema_version":"1.0","entries":[]}')

    from api.pipeline.schemas import CoverageCheck

    async def _uncovered(**_kwargs):
        return CoverageCheck(
            covered=False, matched_rule_id=None, confidence=0.1,
            reason="symptôme distinct",
        )

    with patch(
        "api.pipeline.coverage.check_symptom_coverage", new=AsyncMock(side_effect=_uncovered)
    ), patch(
        "api.pipeline.expand_pack",
        new=AsyncMock(return_value={"new_rules_count": 1, "new_components_count": 0, "total_rules_after": 2}),
    ) as m_expand:
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "USB port delivers no 5V", "owner_ref": "tenant-A"},
        )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0.05))
    finally:
        loop.close()

    assert res.json()["pipeline_kind"] == "expand"
    m_expand.assert_called_once()
    # L'invariant T8 : owner_ref doit être propagé jusqu'à expand_pack pour
    # que l'enrichissement porte added_by_tenant="tenant-A" et non None.
    _, call_kwargs = m_expand.call_args
    assert call_kwargs.get("owner_ref") == "tenant-A", (
        "Branch 3 doit passer owner_ref=request.owner_ref à expand_pack "
        "(trou d'attribution T8 : enrichissement promu sans tenant tracé)"
    )


def test_repairs_branch_none_when_symptom_already_covered(memory_root, client, monkeypatch):
    """Pack complete + coverage classifier says covered with confidence≥0.7
    AND matched_rule_id set → Branch 2: skip, return matched rule."""
    # See sibling test for rationale — the wrapper short-circuits on an
    # empty API key, never reaching the patched coverage call.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake-for-mocked-call")
    monkeypatch.setattr(config_mod, "_settings", None)
    slug_dir = memory_root / "demo-pi"
    slug_dir.mkdir()
    (slug_dir / "registry.json").write_text('{"schema_version":"1.0","device_label":"Demo Pi","components":[],"signals":[]}')
    (slug_dir / "knowledge_graph.json").write_text('{"schema_version":"1.0","nodes":[],"edges":[]}')
    (slug_dir / "rules.json").write_text(
        '{"schema_version":"1.0","rules":[{"id":"rule-charge-001","symptoms":["no charge"],"likely_causes":[{"refdes":"U1","probability":0.9,"mechanism":"x"}],"confidence":0.8}]}'
    )
    (slug_dir / "dictionary.json").write_text('{"schema_version":"1.0","entries":[]}')

    from api.pipeline.schemas import CoverageCheck

    async def _covered(**_kwargs):
        return CoverageCheck(
            covered=True, matched_rule_id="rule-charge-001", confidence=0.92,
            reason="paraphrase of existing rule-charge-001",
        )

    with patch(
        "api.pipeline.coverage.check_symptom_coverage", new=AsyncMock(side_effect=_covered)
    ), patch(
        "api.pipeline.expand_pack", new=AsyncMock()
    ) as m_expand, patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ) as m_pipeline:
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "iPhone won't take a charge"},
        )
    body = res.json()
    assert body["pipeline_kind"] == "none"
    assert body["pipeline_started"] is False
    assert body["matched_rule_id"] == "rule-charge-001"
    assert "paraphrase" in body["coverage_reason"]
    m_expand.assert_not_called()
    m_pipeline.assert_not_called()


def test_repairs_endpoint_rejects_short_input(memory_root, client):
    res = client.post("/pipeline/repairs", data={"device_label": "x", "symptom": "tiny"})
    assert res.status_code == 422


def test_progress_ws_streams_events_from_the_bus(memory_root, client):
    """The WS relays every event published to its slug."""
    with client.websocket_connect("/pipeline/progress/demo-pi") as ws:
        # The server acknowledges the subscription with a "subscribed" frame
        # so the client knows events published from now on will be delivered.
        ack = json.loads(ws.receive_text())
        assert ack == {"type": "subscribed", "device_slug": "demo-pi"}

        async def push():
            # Tiny delay so the WS receive loop is already awaiting.
            await asyncio.sleep(0.05)
            await events.publish("demo-pi", {"type": "phase_started", "phase": "scout"})
            await events.publish("demo-pi", {"type": "pipeline_finished", "status": "APPROVED"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(push())
        finally:
            loop.close()

        ev1 = json.loads(ws.receive_text())
        ev2 = json.loads(ws.receive_text())
        assert ev1 == {"type": "phase_started", "phase": "scout"}
        assert ev2 == {"type": "pipeline_finished", "status": "APPROVED"}


def test_progress_ws_ignores_events_for_other_slugs(memory_root, client):
    with client.websocket_connect("/pipeline/progress/demo-pi") as ws:
        json.loads(ws.receive_text())  # subscribed ack

        async def push():
            await asyncio.sleep(0.05)
            await events.publish("other-device", {"type": "phase_started"})
            await events.publish("demo-pi", {"type": "pipeline_finished"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(push())
        finally:
            loop.close()

        # Only our slug's event arrives.
        ev = json.loads(ws.receive_text())
        assert ev == {"type": "pipeline_finished"}


def _seed_repair(memory_root, slug, *, repair_id, symptom, status, label="Demo Pi", owner_ref=None):
    rdir = memory_root / slug / "repairs"
    rdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "repair_id": repair_id,
        "device_slug": slug,
        "device_label": label,
        "symptom": symptom,
        "status": status,
        "created_at": "2026-04-25T10:00:00+00:00",
    }
    if owner_ref is not None:
        payload["owner_ref"] = owner_ref
    (rdir / f"{repair_id}.json").write_text(json.dumps(payload, ensure_ascii=False))


async def test_cancel_running_pipeline_cancels_task_and_publishes_terminal():
    """cancel_repair cancels the running pipeline task and publishes a terminal
    pipeline_failed(CANCELLED) so subscribers (the cloud progress relay) stop
    waiting — the cooperative-cancel half of the cloud's T5 cancellation (the
    engine holds the only handle on the task).

    Driven by calling the handler directly in one event loop: a background task
    spawned during a TestClient request does not survive to a second request
    (separate loops), so the cross-request HTTP flow can't be asserted here.
    """
    from api.pipeline.routes.repairs import _RUNNING, _register_running, cancel_repair

    slug = "demo-pi"

    async def _hang():
        await asyncio.sleep(30)

    task = asyncio.create_task(_hang())
    _register_running(slug, task)
    assert _RUNNING.get(slug) is task

    result = await cancel_repair(slug)
    assert result == {"cancelled": True, "device_slug": slug}

    # The task was cancelled (and our registry entry cleaned up by the callback).
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled()
    assert _RUNNING.get(slug) is None

    terminal = [
        e for e in events._history.get(slug, [])
        if e.get("type") == "pipeline_failed" and e.get("status") == "CANCELLED"
    ]
    assert terminal, "expected a pipeline_failed(CANCELLED) terminal event on the bus"


def test_cancel_when_nothing_running_returns_false(memory_root, client):
    """Cancelling a slug with no running pipeline is a no-op, not an error
    (idempotent best-effort — the cloud must not 500 on a stale cancel)."""
    res = client.post("/pipeline/repairs/demo-pi/cancel")
    assert res.status_code == 200
    assert res.json()["cancelled"] is False


def test_repairs_endpoint_dedup_reuses_open_ticket(memory_root, client):
    """Resubmitting the same (device, symptom) on an open ticket must NOT
    create a new repair_id nor fire any LLM work — that's the credit-burn
    loop the dedup short-circuit closes."""
    _seed_repair(
        memory_root,
        "demo-pi",
        repair_id="rExisting",
        symptom="pas de boot, écran noir",
        status="open",
    )
    with patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ) as m_pipeline, patch(
        "api.pipeline.expand_pack", new=AsyncMock()
    ) as m_expand, patch(
        "api.pipeline._maybe_check_coverage", new=AsyncMock()
    ) as m_coverage:
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "pas de boot, écran noir"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["repair_id"] == "rExisting"
    assert body["pipeline_started"] is False
    assert body["pipeline_kind"] == "none"
    # Crucial — none of the three LLM paths fired.
    m_pipeline.assert_not_called()
    m_expand.assert_not_called()
    m_coverage.assert_not_called()


def test_repairs_stampede_guard_skips_second_build_when_slug_already_building(memory_root, client):
    """Stampede guard: owner_ref makes two tenants on the same uncached device
    each create their OWN repair — but they must NOT each launch a full pipeline
    for the SHARED pack. A second repair for a slug whose build is already running
    rides the in-flight build instead of starting a duplicate (no double credit),
    and the running task is NOT orphaned (so cancel still reaches it)."""
    from unittest.mock import Mock

    from api.pipeline.routes.repairs import _RUNNING

    running = Mock()
    running.done.return_value = False
    _RUNNING["demo-pi"] = running  # a build is already in flight for this slug
    try:
        with patch(
            "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
        ) as m_pipeline:
            res = client.post(
                "/pipeline/repairs",
                data={"device_label": "Demo Pi", "symptom": "no boot now", "owner_ref": "tenant-B"},
            )
        assert res.status_code == 200
        body = res.json()
        assert body["pipeline_started"] is True  # a build IS running for this slug
        assert len(body["repair_id"]) > 0        # the tenant still gets its own repair
        # The discriminator: the in-flight task was neither replaced nor a 2nd launched.
        assert _RUNNING["demo-pi"] is running
        m_pipeline.assert_not_called()           # no duplicate pipeline kicked off
    finally:
        _RUNNING.pop("demo-pi", None)


def test_repairs_dedup_scoped_by_owner_ref(memory_root, client):
    """Multi-tenant isolation: a repair owned by one owner_ref is NEVER reused
    for another. Two tenants diagnosing the same (device, symptom) each get
    their own repair_id — so their private conversations/measurements never
    collide. (The cloud front-door passes owner_ref = tenant_id.)"""
    _seed_repair(
        memory_root,
        "demo-pi",
        repair_id="rTenantA",
        symptom="pas de boot, écran noir",
        status="open",
        owner_ref="tenant-A",
    )
    with patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ), patch(
        "api.pipeline.expand_pack", new=AsyncMock()
    ), patch(
        "api.pipeline._maybe_check_coverage", new=AsyncMock()
    ):
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "pas de boot, écran noir", "owner_ref": "tenant-B"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["repair_id"] != "rTenantA"  # tenant B gets its OWN repair — no cross-tenant reuse


def test_repairs_dedup_reuses_within_same_owner(memory_root, client):
    """Within the SAME owner the (slug, symptom) dedup still holds — re-submitting
    a tenant's own open ticket must not burn LLM credits."""
    _seed_repair(
        memory_root,
        "demo-pi",
        repair_id="rOwned",
        symptom="pas de boot, écran noir",
        status="open",
        owner_ref="tenant-A",
    )
    with patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ) as m_pipeline, patch(
        "api.pipeline.expand_pack", new=AsyncMock()
    ) as m_expand:
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "pas de boot, écran noir", "owner_ref": "tenant-A"},
        )
    body = res.json()
    assert body["repair_id"] == "rOwned"
    assert body["pipeline_started"] is False
    m_pipeline.assert_not_called()
    m_expand.assert_not_called()


def test_repairs_persists_owner_ref(memory_root, client):
    """A fresh repair records its owner_ref so future dedup is owner-scoped."""
    with patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)):
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "dead pmic now", "owner_ref": "tenant-Z"},
        )
    body = res.json()
    data = json.loads(
        (memory_root / body["device_slug"] / "repairs" / f"{body['repair_id']}.json").read_text()
    )
    assert data["owner_ref"] == "tenant-Z"


def test_repairs_endpoint_dedup_matches_case_insensitive(memory_root, client):
    """Symptom matching is whitespace-trimmed + case-folded, so a
    capitalised resubmit still hits the dedup path."""
    _seed_repair(
        memory_root,
        "demo-pi",
        repair_id="rExisting",
        symptom="pas de boot, écran noir",
        status="in_progress",
    )
    with patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ), patch(
        "api.pipeline.expand_pack", new=AsyncMock()
    ):
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "  Pas de Boot, Écran Noir  "},
        )
    body = res.json()
    assert body["repair_id"] == "rExisting"
    assert body["pipeline_started"] is False


def test_repairs_endpoint_dedup_skipped_when_existing_is_closed(memory_root, client):
    """A closed ticket on the same symptom must NOT block a fresh repair —
    the tech can legitimately reopen a previously-resolved complaint."""
    _seed_repair(
        memory_root,
        "demo-pi",
        repair_id="rResolved",
        symptom="pas de boot, écran noir",
        status="closed",
    )
    with patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ):
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "pas de boot, écran noir"},
        )
    body = res.json()
    assert body["repair_id"] != "rResolved"
    assert body["pipeline_started"] is True


def test_repairs_endpoint_dedup_skipped_on_distinct_symptom(memory_root, client):
    """Same device, different symptom → fresh repair (the existing open
    ticket is for a different problem)."""
    _seed_repair(
        memory_root,
        "demo-pi",
        repair_id="rExisting",
        symptom="pas de boot, écran noir",
        status="open",
    )
    with patch(
        "api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)
    ):
        res = client.post(
            "/pipeline/repairs",
            data={"device_label": "Demo Pi", "symptom": "USB-C ne charge pas"},
        )
    body = res.json()
    assert body["repair_id"] != "rExisting"
    assert body["pipeline_started"] is True


def test_create_repair_multipart_stashes_schematic_and_threads_kind(memory_root):
    with patch("api.pipeline.routes.repairs._run_pipeline_with_events", new=AsyncMock()) as run:
        with TestClient(app) as c:
            r = c.post(
                "/pipeline/repairs",
                data={
                    "device_label": "MSI V311_11",
                    "symptom": "no display output",
                    "device_kind": "laptop_logic_board",
                },
                files={"file": ("sch.pdf", io.BytesIO(b"%PDF-1.4 x"), "application/pdf")},
            )
    assert r.status_code == 200
    slug = r.json()["device_slug"]
    uploads = memory_root / slug / "uploads"
    stashed = list(uploads.glob("*-schematic_pdf-*"))
    assert stashed, "schematic should be stashed into uploads/ before generation"
    assert run.call_args.kwargs["user_device_kind"] == "laptop_logic_board"


def test_create_repair_multipart_without_file_still_works(memory_root):
    with patch("api.pipeline.routes.repairs._run_pipeline_with_events", new=AsyncMock()) as run:
        with TestClient(app) as c:
            r = c.post(
                "/pipeline/repairs",
                data={"device_label": "Some Board XYZ", "symptom": "dead board"},
            )
    assert r.status_code == 200
    assert run.call_args.kwargs["user_device_kind"] is None


def test_create_repair_rejects_invalid_device_kind(memory_root):
    with TestClient(app) as c:
        r = c.post(
            "/pipeline/repairs",
            data={"device_label": "X Board", "symptom": "dead board", "device_kind": "toaster"},
        )
    assert r.status_code == 422
