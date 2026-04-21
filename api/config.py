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
        description="Main reasoning model used by the agent loop.",
    )
    anthropic_model_fast: str = Field(
        default="claude-haiku-4-5",
        description="Fast model for validation / formatting / cheap classification.",
    )

    port: int = Field(default=8000, description="HTTP server port.")
    log_level: str = Field(default="INFO", description="Log level name.")


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
