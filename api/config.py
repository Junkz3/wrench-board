"""Application settings — loaded from environment / .env."""

from __future__ import annotations

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


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
