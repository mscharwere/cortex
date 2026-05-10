"""Runtime config — persona ACL + LiteLLM route view.

Public re-exports:
    Settings   — Pydantic-settings model (reads from environment / .env)
    get_settings — factory; import where settings are needed
"""

from cortex_python.config.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
