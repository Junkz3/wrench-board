"""Diagnostic runtime using Anthropic Managed Agents.

Wire flow:
    browser ⇄ /ws/diagnostic/{slug} ⇄ backend ⇄ MA session event stream

Key SDK contract (see `docs/en/managed-agents/events-and-streaming`):
  - Open the stream **before** sending the first `user.message`, else we
    race against events the server has already emitted.
  - Custom tool handling is two-step. The agent first emits an
    `agent.custom_tool_use` event with full `{id, name, input}`; then the
    session pauses with `session.status_idle` + `stop_reason =
    requires_action`, whose `event_ids` point at the pending tool uses.
    We cache the tool_use events as they arrive so we can look them up
    when `requires_action` fires and send back `user.custom_tool_result`.

This file is a thin re-export shim. The runtime was decomposed into the
``api.agent.runtime`` sub-package; every public name that external
callers rely on (``api.main``, scripts, tests) is re-exported here so
no caller has to update its import path.

Sub-modules look the runtime-collaborator names (``get_settings``,
``AsyncAnthropic``, ``load_managed_ids``, ``get_agent``,
``ensure_*_store``, ``list_conversations``, ``_dispatch_tool``,
``_forward_*_to_*``, ``maybe_auto_seed``) up via this shim's
attributes — that way ``monkeypatch.setattr(rm, "<name>", ...)``
keeps overriding the call sites the way the test suite expects, even
though the source has been split into sibling files.
"""

from __future__ import annotations

# Anthropic SDK + collaborator modules — re-exported so monkeypatching
# them on this module name keeps overriding the sub-module call sites.
from anthropic import AsyncAnthropic

from api.agent._session_mirrors import SessionMirrors as _SessionMirrors
from api.agent.chat_history import (
    ensure_conversation,
    get_conversation_tier,
    list_conversations,
)
from api.agent.managed_ids import get_agent, load_managed_ids
from api.agent.memory_stores import (
    ensure_global_store,
    ensure_memory_store,
    ensure_repair_store,
)
from api.agent.runtime._aux import (
    _MAX_MACRO_BYTES,
    DEFAULT_TIER,
    TierLiteral,
    _active_diagnostic_keys,
    _build_log_id,
    _mirror_jsonl,
    _PendingConv,
    _safe_tool_result_text,
    _sessions_create_with_retry,
    logger,
)
from api.agent.runtime.camera import _dispatch_cam_capture
from api.agent.runtime.dispatch import _dispatch_tool
from api.agent.runtime.forwarders import (
    _forward_session_to_ws,
    _forward_ws_to_session,
)
from api.agent.runtime.handlers import (
    _handle_client_capabilities,
    _handle_client_capture_response,
    _handle_client_protocol_confirmation,
    _handle_client_upload_macro,
)
from api.agent.runtime.protocol import _dispatch_protocol_with_confirmation
from api.agent.runtime.replay import (
    _replay_jsonl_history_to_ws,
    _replay_ma_history_to_ws,
)
from api.agent.runtime.seed import maybe_auto_seed
from api.agent.runtime.session import run_diagnostic_session_managed
from api.agent.runtime.subagents import (
    _run_knowledge_curator,
    _run_subagent_consultation,
)
from api.config import get_settings
from api.session.state import SessionState

__all__ = [
    "AsyncAnthropic",
    "DEFAULT_TIER",
    "SessionState",
    "TierLiteral",
    "_MAX_MACRO_BYTES",
    "_PendingConv",
    "_SessionMirrors",
    "_active_diagnostic_keys",
    "_build_log_id",
    "_dispatch_cam_capture",
    "_dispatch_protocol_with_confirmation",
    "_dispatch_tool",
    "_forward_session_to_ws",
    "_forward_ws_to_session",
    "_handle_client_capabilities",
    "_handle_client_capture_response",
    "_handle_client_protocol_confirmation",
    "_handle_client_upload_macro",
    "_mirror_jsonl",
    "_replay_jsonl_history_to_ws",
    "_replay_ma_history_to_ws",
    "_run_knowledge_curator",
    "_run_subagent_consultation",
    "_safe_tool_result_text",
    "_sessions_create_with_retry",
    "ensure_conversation",
    "ensure_global_store",
    "ensure_memory_store",
    "ensure_repair_store",
    "get_agent",
    "get_conversation_tier",
    "get_settings",
    "list_conversations",
    "load_managed_ids",
    "logger",
    "maybe_auto_seed",
    "run_diagnostic_session_managed",
]
