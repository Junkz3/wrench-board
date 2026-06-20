"""call_with_query_tools: the agentic variant where the model may call a
deterministic `query_graph` tool several times to verify identifiers against the
electrical graph before calling the submit tool.

Used by the pack auditor + revisers so they check refdes/nets against the real
graph before flagging or writing (downstream tasks wire it; this only builds the
helper). Four behaviours are pinned:

  1. query → submit: a query tool_use is answered with a JSON tool_result and the
     loop continues; the submit tool_use validates against the schema and returns.
  2. cap forces submit: once max_query_turns queries are answered, the request is
     re-issued with tool_choice forced to the submit tool (asserted on the
     captured request kwargs).
  3. validation error retries with feedback: an invalid submit payload appends an
     is_error tool_result and retries, capped at max_attempts (then raises).
  4. stats aggregate: PhaseTokenStats accumulates tokens across EVERY API call in
     the loop, not just the final one.

The fake client mirrors the queue-driven _Messages pattern from
test_tool_call_transport_retry.py exactly.
"""
from __future__ import annotations

import json

import pytest

from api.pipeline.schemas import RulesSet
from api.pipeline.telemetry.token_stats import PhaseTokenStats
from api.pipeline.tool_call import call_with_query_tools

pytestmark = pytest.mark.asyncio

_VALID_RULESSET = {
    "schema_version": "1.0",
    "rules": [{
        "id": "R-X-001",
        "symptoms": ["no boot"],
        "likely_causes": [{"refdes": "U1", "probability": 0.5, "mechanism": "short"}],
        "diagnostic_steps": [],
        "confidence": 0.5,
        "sources": [],
    }],
}


class _Block:
    def __init__(self, type, name=None, input=None, id=None):
        self.type = type
        self.name = name
        self.input = input
        self.id = id


class _Usage:
    def __init__(self, input_tokens=10, output_tokens=10):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _Resp:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage or _Usage()


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


_QUERY_TOOL = {
    "name": "query_graph",
    "description": "Look up an identifier in the electrical graph.",
    "input_schema": {"type": "object", "properties": {"refdes": {"type": "string"}}},
}
_SUBMIT_TOOL = {
    "name": "submit_rules",
    "description": "Emit the validated ruleset.",
    "input_schema": {"type": "object"},
}


def _query_block(refdes="U1", id="tu_q"):
    return _Block("tool_use", name="query_graph", input={"refdes": refdes}, id=id)


def _submit_block(payload=None, id="tu_s"):
    return _Block("tool_use", name="submit_rules", input=payload or _VALID_RULESSET, id=id)


def _args(client, **overrides):
    base = dict(
        client=client,
        model="claude-opus-4-8",
        system="sys",
        messages=[{"role": "user", "content": "audit this pack"}],
        query_tool=_QUERY_TOOL,
        query_handler=lambda inp: {"exists": True, "refdes": inp.get("refdes")},
        submit_tool=_SUBMIT_TOOL,
        submit_tool_name="submit_rules",
        output_schema=RulesSet,
        log_label="test",
    )
    base.update(overrides)
    return base


async def test_query_then_submit_answers_query_and_returns_validated():
    """A query tool_use is answered with a JSON tool_result and the loop
    continues; the next turn's submit tool_use validates and returns."""
    calls: list[dict] = []
    handled: list[dict] = []

    def handler(inp):
        handled.append(inp)
        return {"exists": True, "kind": "ic", "refdes": inp["refdes"]}

    client = _Client([
        _Resp([_query_block("U3")]),   # turn 1: model queries the graph
        _Resp([_submit_block()]),      # turn 2: model submits
    ], calls)

    result = await call_with_query_tools(**_args(client, query_handler=handler))

    assert isinstance(result, RulesSet)
    assert result.rules[0].id == "R-X-001"
    # The handler ran with the model's exact query input.
    assert handled == [{"refdes": "U3"}]
    assert len(calls) == 2

    # Both tools are offered every turn (the model picks query OR submit).
    assert calls[0]["tools"] == [_QUERY_TOOL, _SUBMIT_TOOL]
    # Under the cap, tool_choice is "any" (model decides query vs submit).
    assert calls[0]["tool_choice"] == {"type": "any"}

    # The second request carries the conversation grown by the loop: the original
    # user msg, the assistant's query tool_use, and a user tool_result block whose
    # content is the JSON-encoded handler result for that tool_use id.
    msgs = calls[1]["messages"]
    assert msgs[0] == {"role": "user", "content": "audit this pack"}
    assert msgs[1]["role"] == "assistant"
    tr = msgs[2]
    assert tr["role"] == "user"
    block = tr["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_q"
    assert json.loads(block["content"]) == {"exists": True, "kind": "ic", "refdes": "U3"}


async def test_cap_forces_submit_tool_choice():
    """Once max_query_turns queries are answered, the request is re-issued with
    tool_choice forced to the submit tool — even if the model keeps trying to
    query. A query in a forced-submit turn is a protocol miss: re-request, and
    count it against validation attempts so it can't loop forever."""
    calls: list[dict] = []
    # max_query_turns=2 → turns 1 & 2 are free queries (tool_choice "any"); turn 3
    # must be forced to submit. We give it a submit so the forced turn succeeds.
    client = _Client([
        _Resp([_query_block("U1")]),   # turn 1: query (1st, under cap)
        _Resp([_query_block("U2")]),   # turn 2: query (2nd, hits the cap)
        _Resp([_submit_block()]),      # turn 3: forced submit
    ], calls)

    result = await call_with_query_tools(
        **_args(client, max_query_turns=2),
    )

    assert isinstance(result, RulesSet)
    assert len(calls) == 3
    # Turns 1 & 2: under/at the cap → "any" (still allowed to query).
    assert calls[0]["tool_choice"] == {"type": "any"}
    assert calls[1]["tool_choice"] == {"type": "any"}
    # Turn 3: cap exhausted → submit is FORCED.
    assert calls[2]["tool_choice"] == {"type": "tool", "name": "submit_rules"}


async def test_validation_error_retries_with_is_error_feedback_then_raises():
    """An invalid submit payload appends an is_error tool_result carrying the
    validation error and retries; total submit-validation attempts are capped at
    max_attempts, then it raises (same exception style as call_with_forced_tool)."""
    calls: list[dict] = []
    bad = {"schema_version": "1.0", "rules": "not a list"}  # fails validation
    client = _Client([
        _Resp([_submit_block(bad)]),   # attempt 1: invalid
        _Resp([_submit_block(bad)]),   # attempt 2: invalid again
    ], calls)

    with pytest.raises(RuntimeError, match="submit_rules"):
        await call_with_query_tools(**_args(client, max_attempts=2))

    assert len(calls) == 2  # exactly max_attempts validation tries
    # The retry conversation surfaced the failure as an is_error tool_result for
    # the assistant's submit tool_use, so the model can self-correct.
    retry_msgs = calls[1]["messages"]
    err_block = retry_msgs[-1]["content"][0]
    assert err_block["type"] == "tool_result"
    assert err_block["tool_use_id"] == "tu_s"
    assert err_block["is_error"] is True
    assert "alid" in err_block["content"]  # "Validation"/"invalid" — the error text


async def test_validation_error_recovers_then_succeeds():
    """A first invalid submit is fed back; the model corrects it on retry and we
    return the validated model (attempt budget not exhausted)."""
    calls: list[dict] = []
    bad = {"schema_version": "1.0", "rules": "not a list"}
    client = _Client([
        _Resp([_submit_block(bad)]),
        _Resp([_submit_block(_VALID_RULESSET)]),
    ], calls)

    result = await call_with_query_tools(**_args(client, max_attempts=2))
    assert isinstance(result, RulesSet)
    assert len(calls) == 2


async def test_stats_aggregate_across_every_turn():
    """PhaseTokenStats accumulates input/output tokens across EVERY API call in
    the loop (two queries + one submit = three calls), not just the final one."""
    calls: list[dict] = []
    client = _Client([
        _Resp([_query_block("U1")], usage=_Usage(input_tokens=100, output_tokens=20)),
        _Resp([_query_block("U2")], usage=_Usage(input_tokens=200, output_tokens=30)),
        _Resp([_submit_block()], usage=_Usage(input_tokens=300, output_tokens=40)),
    ], calls)

    stats = PhaseTokenStats(phase="audit")
    result = await call_with_query_tools(**_args(client, stats=stats))

    assert isinstance(result, RulesSet)
    assert len(calls) == 3
    assert stats.input_tokens == 600   # 100 + 200 + 300
    assert stats.output_tokens == 90   # 20 + 30 + 40
    assert stats.call_count == 3


# --- regression: every parallel tool_use MUST be answered (no orphaned blocks) --
#
# The Anthropic API requires a tool_result for EVERY tool_use block of the
# preceding assistant turn. Opus under tool_choice="any" routinely emits parallel
# tool calls; a loop that extracts ONE block via next() and answers only that one
# leaves the rest orphaned → the NEXT request 400s. These three tests pin the
# three concrete 400-producing paths the old single-block code allowed.


async def test_parallel_query_blocks_all_answered():
    """Two parallel query tool_use blocks in one response must BOTH get a
    tool_result in the single following user message (matched by id), the handler
    runs once per block, and the loop then submits successfully."""
    calls: list[dict] = []
    handled: list[dict] = []

    def handler(inp):
        handled.append(inp)
        return {"exists": True, "refdes": inp["refdes"]}

    client = _Client([
        # turn 1: the model fires TWO query_graph calls in parallel (distinct ids).
        _Resp([_query_block("U1", id="tu_q1"), _query_block("U2", id="tu_q2")]),
        _Resp([_submit_block()]),  # turn 2: submit
    ], calls)

    result = await call_with_query_tools(**_args(client, query_handler=handler))

    assert isinstance(result, RulesSet)
    assert handled == [{"refdes": "U1"}, {"refdes": "U2"}]  # both, in order

    # The user turn after the parallel assistant turn must answer BOTH tool_uses.
    msgs = calls[1]["messages"]
    assert msgs[1]["role"] == "assistant"     # the parallel tool_use turn
    tool_results = msgs[2]["content"]
    assert msgs[2]["role"] == "user"
    assert [b["type"] for b in tool_results] == ["tool_result", "tool_result"]
    assert {b["tool_use_id"] for b in tool_results} == {"tu_q1", "tu_q2"}
    # Each result carries its own handler output keyed to its own block.
    by_id = {b["tool_use_id"]: json.loads(b["content"]) for b in tool_results}
    assert by_id["tu_q1"] == {"exists": True, "refdes": "U1"}
    assert by_id["tu_q2"] == {"exists": True, "refdes": "U2"}


async def test_simultaneous_query_and_submit_invalid():
    """A response carrying BOTH a query block and an INVALID submit block: the next
    user turn must answer BOTH — a normal tool_result for the query and an is_error
    tool_result for the submit — so neither is orphaned. A valid submit then ends it."""
    calls: list[dict] = []
    handled: list[dict] = []
    bad = {"schema_version": "1.0", "rules": "not a list"}  # fails validation

    def handler(inp):
        handled.append(inp)
        return {"exists": True, "refdes": inp["refdes"]}

    client = _Client([
        # turn 1: parallel query + invalid submit in one assistant response.
        _Resp([_query_block("U7", id="tu_q1"), _submit_block(bad, id="tu_s1")]),
        _Resp([_submit_block(_VALID_RULESSET)]),  # turn 2: corrected, valid submit
    ], calls)

    result = await call_with_query_tools(**_args(client, query_handler=handler))

    assert isinstance(result, RulesSet)
    # The query block was honoured (handler ran) even though it shared the turn
    # with a submit — its result still has to exist or the next call 400s.
    assert handled == [{"refdes": "U7"}]

    follow_up = calls[1]["messages"][-1]["content"]
    assert follow_up == [
        # NB: query result first, submit is_error second — every block answered.
        {"type": "tool_result", "tool_use_id": "tu_q1",
         "content": json.dumps({"exists": True, "refdes": "U7"}, ensure_ascii=False)},
        {"type": "tool_result", "tool_use_id": "tu_s1", "is_error": True,
         "content": follow_up[1]["content"]},  # exact error text not pinned
    ]
    assert follow_up[1]["is_error"] is True
    assert "alid" in follow_up[1]["content"]


async def test_protocol_miss_with_tool_use_gets_stub_results():
    """Protocol miss with an orphan-able tool_use: cap=0 forces submit immediately,
    but the model returns a query block anyway. The follow-up user message must
    carry an is_error tool_result stub for that block id (no orphan) — and the loop
    still terminates by raising after max_attempts."""
    calls: list[dict] = []
    client = _Client([
        # cap=0 → submit forced from turn 1, yet the model queries instead.
        _Resp([_query_block("U9", id="tu_orphan1")]),
        _Resp([_query_block("U9", id="tu_orphan2")]),  # misbehaves again → 2nd miss
    ], calls)

    with pytest.raises(RuntimeError, match="submit_rules"):
        await call_with_query_tools(
            **_args(client, max_query_turns=0, max_attempts=2),
        )

    assert len(calls) == 2  # capped at max_attempts — bounded termination held
    # The user turn after the orphan-able query MUST answer that tool_use, or the
    # 2nd request would 400. A stub is_error tool_result keeps the protocol legal.
    follow_up = calls[1]["messages"][-1]["content"]
    stub = next(b for b in follow_up if b.get("type") == "tool_result")
    assert stub["tool_use_id"] == "tu_orphan1"
    assert stub["is_error"] is True


async def test_misrouted_query_payload_in_submit_is_rerouted_not_failed():
    """Opus under tool_choice='any' sometimes calls the SUBMIT tool with a
    query_graph payload (e.g. {refdes:...} / {op:...}). That is a misrouted
    query, NOT a failed submit: it must be run through the query handler and the
    loop continue — WITHOUT burning a submit attempt. With max_attempts=1, the
    old behaviour (count it as a failed submit) would raise immediately."""
    calls: list[dict] = []
    handled: list[dict] = []

    def handler(inp):
        handled.append(inp)
        return {"exists": True, "refdes": inp.get("refdes")}

    client = _Client([
        _Resp([_submit_block(payload={"refdes": "U9"}, id="tu_misroute")]),  # query in disguise
        _Resp([_submit_block()]),  # then a real submit
    ], calls)

    result = await call_with_query_tools(
        **_args(client, query_handler=handler, max_attempts=1)
    )

    assert isinstance(result, RulesSet)
    # The misrouted payload was handled as a query, not a failed submit.
    assert handled == [{"refdes": "U9"}]
    # The misrouted submit_use id was answered (no orphan → no 400 on retry).
    follow_up = calls[1]["messages"][-1]["content"]
    answered_ids = {b["tool_use_id"] for b in follow_up if b.get("type") == "tool_result"}
    assert "tu_misroute" in answered_ids


async def test_misrouted_query_in_submit_rerouted_within_grace_after_cap():
    """After the query cap is reached the model is FORCED to submit, but a dense
    pack often makes it route ONE more graph verification into the submit tool
    (a query-shaped payload). Punishing that (the old `not cap_reached` gate)
    starved the reviser into a no-op — the exact 12-Pro-Max failure. A small
    post-cap grace re-routes the disguised query through the handler so the model
    gets its answer and can then submit. With max_attempts=1 the OLD behaviour
    (count it as a failed submit) would raise immediately."""
    calls: list[dict] = []
    handled: list[dict] = []

    def handler(inp):
        handled.append(inp)
        return {"exists": True, "refdes": inp.get("refdes")}

    client = _Client([
        _Resp([_query_block("U1")]),                                  # turn 1: query → cap hit
        _Resp([_submit_block(payload={"refdes": "U2"}, id="tu_mis")]),  # turn 2: forced submit, query in disguise
        _Resp([_submit_block()]),                                     # turn 3: real valid submit
    ], calls)

    result = await call_with_query_tools(
        **_args(client, query_handler=handler, max_query_turns=1, max_attempts=1)
    )

    assert isinstance(result, RulesSet)
    # Both queries ran (the post-cap one was re-routed, not failed) — proof the
    # grace answered it: with max_attempts=1 a failed submit would have raised.
    assert handled == [{"refdes": "U1"}, {"refdes": "U2"}]
    # Turn 2 was forced to submit (cap exhausted) yet still got re-routed.
    assert calls[1]["tool_choice"] == {"type": "tool", "name": "submit_rules"}


async def test_post_cap_query_reroute_grace_is_bounded():
    """The post-cap re-route grace must be finite: a model that ONLY ever sends
    query-shaped payloads to the submit tool after the cap still terminates by
    raising, never loops forever."""
    calls: list[dict] = []
    responses = [_Resp([_query_block("U0")])]  # turn 1: query → cap (max_query_turns=1)
    # Then it ALWAYS misroutes a query into submit, never a real submit.
    responses += [
        _Resp([_submit_block(payload={"refdes": f"U{i}"}, id=f"m{i}")]) for i in range(30)
    ]
    client = _Client(responses, calls)

    with pytest.raises(RuntimeError, match="submit_rules"):
        await call_with_query_tools(**_args(client, max_query_turns=1, max_attempts=2))

    # Bounded: cap + finite grace re-routes + max_attempts misses, far below 30.
    assert len(calls) < 30
