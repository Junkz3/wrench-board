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
        default=1.0,
        ge=0.0,
        le=10.0,
        description=(
            "Seconds to wait between dispatching writer 1 and writers 2+3, so Anthropic "
            "materializes the cache entry before the parallel readers arrive."
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
    # Memory stores entered Anthropic's public beta on 2026-04-23. With the
    # flag on (default), pipeline output is pre-seeded into each device's
    # store and diagnostic sessions write findings back. Set to False in
    # .env to fully bypass memory_stores (e.g. for offline dev or if the
    # workspace loses access). All call sites degrade gracefully either way.
    ma_memory_store_enabled: bool = Field(
        default=True,
        description=(
            "Gate for Anthropic Managed Agents memory_stores integration. "
            "On since public beta (2026-04-23); set False to disable."
        ),
    )
    chat_history_backend: Literal["jsonl", "managed_agents"] = Field(
        default="jsonl",
        description=(
            "Where diagnostic chat history lives. 'jsonl' writes one line per "
            "message event under memory/{slug}/repairs/{id}/messages.jsonl — "
            "works today without any Anthropic feature gate. 'managed_agents' "
            "will defer replay to native MA sessions when the preview lands "
            "(same pattern as ma_memory_store_enabled)."
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


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
