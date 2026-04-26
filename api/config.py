# SPDX-License-Identifier: Apache-2.0
"""Application settings — loaded from environment / .env."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the microsolder-agent backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    anthropic_api_key: str = Field(
        default="",
        description="Anthropic API key. Required at agent runtime, optional for tests.",
    )
    anthropic_model_main: str = Field(
        default="claude-opus-4-7",
        description=(
            "Top-tier reasoning model. Pipeline roles: Cartographe, Clinicien, "
            "Auditor. Diagnostic 'deep' tier."
        ),
    )
    anthropic_model_fast: str = Field(
        default="claude-haiku-4-5",
        description="Reserved for lightweight classification / formatting tasks.",
    )
    anthropic_model_sonnet: str = Field(
        default="claude-sonnet-4-6",
        description=(
            "Mid-tier model. Pipeline roles: Scout, Registry Builder, "
            "Lexicographe — structured extraction without heavy synthesis."
        ),
    )

    port: int = Field(default=8000, description="HTTP server port.")
    log_level: str = Field(default="INFO", description="Log level name.")

    # --- CORS -----------------------------------------------------------------
    # Default covers local workbench use (:8000 same-origin + common dev
    # ports). Override via CORS_ALLOW_ORIGINS="url1,url2,..." for remote
    # access. "*" is still accepted but discouraged — it degrades to permissive
    # mode without credentials since the wildcard + credentials combo is
    # rejected by browsers regardless of server config.
    cors_allow_origins: str = Field(
        default="http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,http://127.0.0.1:5173",
        description=(
            "Comma-separated CORS origins. Use * only for ad-hoc external access."
        ),
    )

    # --- Upload hardening -----------------------------------------------------
    # .kicad_pcb files for full boards can exceed 100 MB (MNT Reform is ~25 MB,
    # larger mainboards push past 100 MB). 200 MB leaves headroom while protecting
    # /tmp and RAM from a malicious oversized upload on POST /api/board/parse.
    board_upload_max_bytes: int = Field(
        default=200 * 1024 * 1024,
        ge=1,
        description=(
            "Maximum accepted size in bytes for POST /api/board/parse uploads. "
            "Requests exceeding this cap are rejected with 413 before parsing."
        ),
    )

    # --- Pipeline V2 settings -------------------------------------------------
    memory_root: str = Field(
        default="memory",
        description="Root directory under which per-device knowledge packs are written.",
    )
    pipeline_max_revise_rounds: int = Field(
        default=1,
        ge=0,
        le=3,
        description=(
            "Maximum number of audit→revise→re-audit rounds before accepting the pack "
            "with residual issues. Values > 2 are reserved for debug."
        ),
    )
    pipeline_cache_warmup_seconds: float = Field(
        default=3.0,
        ge=0.0,
        le=10.0,
        description=(
            "Seconds to wait between dispatching writer 1 (Cartographe) and writers 2+3 "
            "(Clinicien + Lexicographe), so Anthropic materializes the ephemeral cache "
            "entry before the parallel readers arrive. Observed cache materialization "
            "takes 2–3s; 1.0s was too aggressive and caused cache misses with subsequent "
            "token re-writes."
        ),
    )
    pipeline_scout_min_symptoms: int = Field(
        default=3,
        ge=0,
        description="Minimum distinct **Symptom:** blocks the Scout dump must contain.",
    )
    pipeline_scout_min_components: int = Field(
        default=3,
        ge=0,
        description=(
            "Minimum distinct components cited in the Scout dump (sum of unique "
            "canonical names and refdes across all symptom blocks and the components "
            "section)."
        ),
    )
    pipeline_scout_min_sources: int = Field(
        default=3,
        ge=0,
        description="Minimum distinct source URLs cited in the Scout dump.",
    )
    pipeline_scout_max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        description=(
            "How many extra Scout attempts when the first dump falls below the "
            "pipeline_scout_min_* thresholds. Each retry broadens the search scope."
        ),
    )

    # --- Managed Agents memory stores -----------------------------------------
    # With the flag on (default), pipeline output is pre-seeded into each
    # device's store and diagnostic sessions write findings back. Set to
    # False in .env to fully bypass memory_stores (e.g. for offline dev or
    # if the workspace loses access). All call sites degrade gracefully
    # either way.
    ma_memory_store_enabled: bool = Field(
        default=True,
        description=(
            "Gate for Anthropic Managed Agents memory_stores integration. "
            "Set False to disable (offline dev, restricted workspace)."
        ),
    )
    chat_history_backend: Literal["jsonl", "managed_agents"] = Field(
        default="jsonl",
        description=(
            "Where diagnostic chat history lives. 'jsonl' writes one line per "
            "message event under memory/{slug}/repairs/{id}/messages.jsonl. "
            "'managed_agents' defers replay to native MA sessions."
        ),
    )

    # --- Anthropic client resilience ------------------------------------------
    # Default SDK max_retries (2) tolerates ~6s of backoff before bubbling.
    # Real overload incidents last 30s–2min; 5 retries gives ~62s of
    # exponential-backoff tolerance (2+4+8+16+32s) before propagating the error.
    # Override via ANTHROPIC_MAX_RETRIES in .env if needed.
    anthropic_max_retries: int = Field(
        default=5,
        ge=0,
        description=(
            "Anthropic SDK retry count for transient 5xx / 529 overload responses. "
            "Raised from the SDK default of 2 to survive short overload windows."
        ),
    )

    # --- Managed Agents stream watchdog ---------------------------------------
    # Inactivity timeout on `client.beta.sessions.events.stream(...)`. The
    # async iterator can block indefinitely if Anthropic's SSE stalls without
    # closing the TCP connection (TCP keepalive ~9 min by default). The
    # watchdog timeouts the stream and emits a `stream_timeout` WS event so
    # the frontend can surface "session lost — please reconnect" instead of
    # showing an infinite spinner. 600 s (10 min) is generous: Opus + adaptive
    # thinking on a complex turn can spend 1-2 min before its first event.
    ma_stream_event_timeout_seconds: float = Field(
        default=600.0,
        gt=0,
        description=(
            "Per-event inactivity timeout on the MA SSE event stream. "
            "If no event arrives within this window, the stream is closed "
            "cleanly and a stream_timeout WS event is sent to the frontend."
        ),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
