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
    # NOTE: this module now sources NOTHING from the environment. Both fields
    # that used to live here were removed on 2026-09-16, for opposite reasons.
    #
    # CORTEX_VACUUMOPS_DRY_RUN — DELETED, not migrated. It was already dead: the
    # comment that sat on it ("Global override removed; per-unit DB flags
    # control") was accurate, and tracing the call chain confirmed it. The value
    # reached exactly ONE line of code — a startup log field in
    # vacuumops_loop() — while every dispatch decision read
    # `effective_dry_run = unit_dry_runs.get(robot, True)` from the per-unit
    # `vac_units.dry_run` column. Commit bb0d47b deliberately removed the global
    # override because an env var silently OR-ing with a DB flag produces
    # confusing state; this field was its leftover shell.
    #
    # It was considered for migration to `cortex_vacuumops_settings` alongside
    # the prior learner and REJECTED by Carlos (2026-09-16). Migrating it would
    # have resurrected the exact override bb0d47b removed, and put a switch
    # labelled "dry run" in the HomeOps UI that changed nothing but a log line —
    # a veto dressed as a switch, which is precisely the 2026-09-11 failure
    # (a DB kill switch that was inert for seven days while the UI agreed it was
    # on). A control that does not control is worse than no control.
    #
    # CORTEX_VACUUMOPS_PRIOR_LEARNER_ENABLED — MIGRATED to the DB. It is now
    # `cortex_vacuumops_settings.prior_learner_enabled`, read fresh every loop
    # tick via HomeOpsAdapter.get_vacuumops_settings() and threaded in per-tick,
    # exactly as mop_enabled and opportunity_actuate are. Unlike those two it
    # fails OPEN (defaults True on any read failure) — Carlos's explicit
    # exception: the learner gates no physical action, so the cost of it running
    # spuriously is a handful of HA history calls, while the cost of it NOT
    # running is that its priors FREEZE WITHOUT LOSING CONFIDENCE and a live
    # withhold rule keeps reading them.
    #
    # That argument is what changed the field's home — though not in the form it
    # was first written. The claim that pausing the learner loses UNRECOVERABLE
    # time is false: priors.py's watermark catch-up plus
    # `prior_learner_backfill_days = 28` refill any gap under 28 days from HA
    # recorder history.
    #
    # The real hazard is that stale priors never lose confidence —
    # `confidence_for()` is count-based with no recency term — so a paused
    # learner keeps reporting "good" on frozen data while r1.opportunity_check
    # goes on withholding dispatches from it. That is a switch you may need to
    # fix in seconds, which is precisely what an env var could not offer.
    #
    # `extra = "ignore"` above means a stale CORTEX_VACUUMOPS_DRY_RUN or
    # CORTEX_VACUUMOPS_PRIOR_LEARNER_ENABLED left in an old .env is harmlessly
    # ignored rather than a startup error.
    #
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
