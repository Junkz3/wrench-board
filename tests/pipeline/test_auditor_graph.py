"""Graph-grounded auditor (Task 7).

On the real macbook-air-m1 build the auditor — anchored only on the thin
web-derived registry — flagged REAL schematic identifiers (SWV011, the PP1V2_S2
rail sourced by U8100) as fabricated, and its revision briefs demanded "fixes"
that would have corrupted true facts. The fix gives the auditor, ONLY when a
graph exists, two new surfaces:

  (a) a deterministic ground-truth report covering every identifier the writers
      mentioned (`ground_truth_report`), injected into the cached context block
      right after the precomputed-drift section, and
  (b) the `query_graph` tool so it can VERIFY existence/voltage/source before
      accusing — wired through the capped agentic `call_with_query_tools` loop.

Web-only packs (`graph_truth is None`) keep TODAY'S EXACT path: a single forced
`submit_audit_verdict` call, no ground-truth block, no query tool. These three
tests pin both branches + that the query handler really hits GraphTruth.

Fake-client patterns reuse `test_auditor_cache.py` (kwarg-capturing fake of
`call_with_forced_tool`) and `test_tool_call_query.py` (queue-driven `_Messages`
streaming fake for the agentic loop) verbatim — see those files.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from api.pipeline.graph_truth import GraphTruth
from api.pipeline.schemas import (
    AuditVerdict,
    Dictionary,
    KnowledgeGraph,
    Registry,
    RulesSet,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
    TypedEdge,
)

pytestmark = pytest.mark.asyncio


# --- fixtures ---------------------------------------------------------------
#
# Mirrors the `_mini_graph()` style from tests/pipeline/test_graph_truth.py: a
# tiny board with the exact real-world refdes the web registry never sees
# (SWV011) and the U8100→PP1V2_S2 rail chain the auditor wrongly flagged.


def _mini_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="mini",
        components={
            "U8100": ComponentNode(refdes="U8100", type="ic", kind="ic", role="pmic", pages=[3]),
            "SWV011": ComponentNode(refdes="SWV011", type="switch", role="load_switch", pages=[5]),
        },
        nets={"PP1V2_S2": NetNode(label="PP1V2_S2", is_power=True)},
        power_rails={
            "PP1V2_S2": PowerRail(
                label="PP1V2_S2",
                voltage_nominal=1.2,
                source_refdes="U8100",
                consumers=[],
            ),
        },
        typed_edges=[TypedEdge(src="U8100", dst="PP1V2_S2", kind="powers")],
        quality=SchematicQualityReport(total_pages=5, pages_parsed=5),
    )


def _empty_pack():
    """A minimal valid pack so the use-case can render its context blocks."""
    return dict(
        device_label="Demo",
        registry=Registry(device_label="Demo", components=[], signals=[]),
        knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
        precomputed_drift=[],
    )


def _approved_verdict() -> AuditVerdict:
    return AuditVerdict(
        overall_status="APPROVED",
        consistency_score=1.0,
        drift_report=[],
        files_to_rewrite=[],
        revision_brief="",
    )


# --- streaming fake for the agentic query loop ------------------------------
#
# Identical queue-driven _Messages pattern to test_tool_call_query.py — one
# response per stream() call, returned via get_final_message().


class _Block:
    def __init__(self, type, name=None, input=None, id=None):
        self.type = type
        self.name = name
        self.input = input
        self.id = id


class _Usage:
    def __init__(self):
        self.input_tokens = 10
        self.output_tokens = 10
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _Resp:
    def __init__(self, content):
        self.content = content
        self.usage = _Usage()


class _Stream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self._resp


class _Messages:
    def __init__(self, responses, calls):
        self._responses = responses
        self._calls = calls

    def stream(self, **kwargs):
        self._calls.append(kwargs)
        return _Stream(self._responses[len(self._calls) - 1])


class _Client:
    def __init__(self, responses, calls):
        self.messages = _Messages(responses, calls)


_VALID_VERDICT_PAYLOAD = {
    "schema_version": "1.0",
    "overall_status": "APPROVED",
    "consistency_score": 1.0,
    "files_to_rewrite": [],
    "drift_report": [],
    "revision_brief": "",
}


# --- 1. no graph → today's exact path ---------------------------------------


async def test_without_graph_behaviour_unchanged():
    """`graph_truth=None` keeps the single forced-tool path: exactly ONE API call,
    its `tools` list has ONLY the submit tool, and the rendered context carries NO
    ground-truth block — byte-for-byte the legacy web-only auditor."""
    from api.pipeline import auditor as auditor_mod

    captured = {}

    async def fake_forced(*, client, model, system, messages, tools, **kw):
        captured["tools"] = tools
        captured["messages"] = messages
        captured["calls"] = captured.get("calls", 0) + 1
        return _approved_verdict()

    # Save/restore so a module-level monkeypatch never bleeds into another test
    # (test 3 drives the REAL loop and must see the unpatched dispatch).
    original = auditor_mod.call_with_forced_tool
    auditor_mod.call_with_forced_tool = fake_forced  # type: ignore[attr-defined]
    try:
        await auditor_mod.run_auditor(
            client=MagicMock(),
            model="claude-opus-4-8",
            graph_truth=None,
            ground_truth_report=None,
            **_empty_pack(),
        )
    finally:
        auditor_mod.call_with_forced_tool = original  # type: ignore[attr-defined]

    # Exactly one call, only the submit tool offered.
    assert captured["calls"] == 1
    assert [t["name"] for t in captured["tools"]] == ["submit_audit_verdict"]
    # No ground-truth block anywhere in the rendered context.
    rendered = captured["messages"][0]["content"][0]["text"]
    assert "Schematic ground truth" not in rendered


# --- 2. graph present → report injected + query tool registered -------------


async def test_with_graph_report_injected_and_tool_registered():
    """With a GraphTruth + report, dispatch routes through `call_with_query_tools`:
    BOTH `query_graph` and `submit_audit_verdict` are offered, and the cached
    context carries the `# Schematic ground truth` block (with the report text)
    BEFORE the registry section."""
    from api.pipeline import auditor as auditor_mod

    captured = {}

    async def fake_query(*, client, model, system, messages, query_tool, submit_tool, **kw):
        captured["tools"] = [query_tool, submit_tool]
        captured["messages"] = messages
        return _approved_verdict()

    # Save/restore so this fake doesn't bleed into test 3 (which drives the REAL
    # loop through `call_with_query_tools`).
    original = auditor_mod.call_with_query_tools
    auditor_mod.call_with_query_tools = fake_query  # type: ignore[attr-defined]

    report = "- component SWV011: present (switch)\n- rail/net PP1V2_S2: present — 1.2 V"

    try:
        await auditor_mod.run_auditor(
            client=MagicMock(),
            model="claude-opus-4-8",
            graph_truth=GraphTruth(_mini_graph()),
            ground_truth_report=report,
            **_empty_pack(),
        )
    finally:
        auditor_mod.call_with_query_tools = original  # type: ignore[attr-defined]

    # Both tools are wired through the query loop.
    names = {t["name"] for t in captured["tools"]}
    assert names == {"query_graph", "submit_audit_verdict"}

    # The ground-truth block + the report text are in the cached context block,
    # positioned BEFORE the registry section (the report frames the registry).
    rendered = captured["messages"][0]["content"][0]["text"]
    assert "# Schematic ground truth" in rendered
    assert report in rendered
    assert rendered.index("# Schematic ground truth") < rendered.index("# Registry")


# --- 3. query handler really answers from the graph -------------------------


async def test_query_handler_answers_from_graph():
    """When the model emits a `query_graph` tool_use for SWV011 then a valid
    submit, the handler must really hit GraphTruth (the tool_result fed back to
    the model carries `"present": true`), and the verdict is returned."""
    from api.pipeline import auditor as auditor_mod

    calls: list[dict] = []
    client = _Client(
        [
            # turn 1: the model verifies SWV011 against the graph before judging.
            _Resp([_Block("tool_use", name="query_graph",
                          input={"op": "component", "name": "SWV011"}, id="tu_q")]),
            # turn 2: it submits a valid verdict.
            _Resp([_Block("tool_use", name="submit_audit_verdict",
                          input=_VALID_VERDICT_PAYLOAD, id="tu_s")]),
        ],
        calls,
    )

    verdict = await auditor_mod.run_auditor(
        client=client,
        model="claude-opus-4-8",
        graph_truth=GraphTruth(_mini_graph()),
        ground_truth_report="(report)",
        **_empty_pack(),
    )

    assert isinstance(verdict, AuditVerdict)
    assert verdict.overall_status == "APPROVED"

    # The 2nd request carries the tool_result the loop fed back for the query —
    # proof the handler really resolved SWV011 against GraphTruth.
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "tu_q"
    answer = json.loads(tool_result["content"])
    assert answer["present"] is True


async def test_previous_brief_framed_as_already_applied(monkeypatch):
    """Anti-ancrage (vu au re-run réel macbook-air-m1) : au round 2 l'auditor a
    répété mot pour mot des reproches DÉJÀ corrigés sur disque (rail fantôme
    remplacé, C81DC renommé, N-DP800 ajouté) et a baissé le score 0.62→0.55 →
    early-stop → échec. Le brief précédent doit être présenté comme « déjà
    appliqué, à re-vérifier », jamais comme une liste à recopier."""
    import api.pipeline.auditor as auditor_mod
    from api.pipeline.schemas import AuditVerdict

    captured = {}

    async def fake_call(*, messages, **kw):
        captured["messages"] = messages
        return AuditVerdict(
            overall_status="APPROVED", consistency_score=1.0,
            drift_report=[], files_to_rewrite=[], revision_brief="",
        )

    monkeypatch.setattr(auditor_mod, "call_with_forced_tool", fake_call)
    await auditor_mod.run_auditor(
        client=object(),
        model="claude-opus-4-8",
        device_label="Demo",
        registry=Registry(device_label="Demo", components=[], signals=[]),
        knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
        precomputed_drift=[],
        revision_brief="fix the phantom rail",
    )
    directive = captured["messages"][0]["content"][1]["text"]
    assert "ALREADY been applied" in directive
    assert "re-verify" in directive.lower()
    assert "fix the phantom rail" in directive
    # le score doit refléter l'état COURANT, pas l'historique
    assert "current files" in directive.lower()
