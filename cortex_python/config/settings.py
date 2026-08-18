"""CORTEX-Python — runtime settings.

Pydantic-settings reads from environment variables (or a .env file when
running locally).  Every variable name here is the canonical reference for
.env / .env.example.

Spec: ``C:/Jarvis/Team/TARS/cortex_architecture.md`` (v3.1) §4, §4.1.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str  # e.g. mysql+aiomysql://cortex:pw@mariadb:3306/cortex

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str  # e.g. redis://:password@redis:6379/0

    # ── LiteLLM (MS-S1 MAX inference proxy) ───────────────────────────────────
    # Canonical hostname: ollama.perwnet.com (per reference_ollama_perwnet_hostname.md)
    litellm_base_url: str = "http://ollama.perwnet.com:4000"
    litellm_api_key: str = ""  # bearer token for LiteLLM proxy auth

    # ── CORTEX internal ────────────────────────────────────────────────────────
    cortex_secret_key: str  # HMAC / internal auth (§7 REST auth)
    cortex_env: str = "production"  # production | development

    # ── HomeOps integration ────────────────────────────────────────────────────
    homeops_base_url: str = "http://192.168.30.4:4000"
    cortex_api_key: str = ""  # Bearer token for CORTEX → HomeOps calls

    # ── Home Assistant ─────────────────────────────────────────────────────────
    homeassistant_url: str = ""  # e.g. https://homeassistant.perwnet.com:8123
    homeassistant_token: str = ""  # long-lived HA token

    # ── VacuumOps module ───────────────────────────────────────────────────────
    cortex_vacuumops_dry_run: bool = False  # Global override removed; per-unit DB flags control
    # NOTE: CORTEX_VACUUMOPS_MOP_ENABLED (the mop-cadence gate master kill switch)
    # intentionally has NO field here as of 2026-08-18. It is now a live,
    # DB-backed setting (HomeOps cortex_vacuumops_settings, GET/PATCH
    # /api/cortex/vacuumops-settings) read fresh every loop tick via
    # HomeOpsAdapter.get_vacuumops_mop_enabled() — not sourced from the
    # environment at all, matching the per-unit dry_run precedent (commit
    # bb0d47b removed that field's analogous env-var override for the same
    # reason: two sources of truth for a safety-critical toggle produce
    # confusing state). `extra = "ignore"` above means a stale
    # CORTEX_VACUUMOPS_MOP_ENABLED left in an old .env is harmlessly ignored,
    # not a startup error. See modules/vacuumops/config.py's mop_enabled
    # field docstring for the full reasoning.

    # ── Service behaviour ──────────────────────────────────────────────────────
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _validate_db_url(cls, v: str) -> str:
        if not v.startswith(("mysql", "mariadb", "sqlite")):
            raise ValueError("DATABASE_URL must be a MariaDB/MySQL or SQLite URL")
        return v

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, v: str) -> str:
        if not v.startswith("redis://"):
            raise ValueError("REDIS_URL must start with redis://")
        return v


def get_settings() -> Settings:
    """Return application settings.  Import and call where needed.

    Required fields (database_url, redis_url, cortex_secret_key) are resolved
    from environment variables by pydantic-settings at runtime.  The
    type: ignore suppresses mypy's static-call-arg check which does not
    understand the pydantic-settings env-var injection pattern.
    """
    return Settings()  # type: ignore[call-arg]
