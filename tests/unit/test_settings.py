"""Unit tests for CORTEX-Python settings module.

Phase 0 Item 2 smoke: verifies Settings fields, validators, and
get_settings() are importable and behave correctly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cortex_python.config.settings import Settings


class TestSettingsValidators:
    """Test Pydantic field validators in Settings."""

    def _make(self, **overrides: str) -> Settings:
        """Build a valid Settings instance with overrides."""
        defaults = {
            "database_url": "mysql+aiomysql://cortex:pw@mariadb:3306/cortex",
            "redis_url": "redis://:password@redis:6379/0",
            "cortex_secret_key": "a" * 64,
        }
        defaults.update(overrides)
        return Settings.model_validate(defaults)

    def test_valid_settings(self) -> None:
        s = self._make()
        assert s.database_url.startswith("mysql")
        assert s.redis_url.startswith("redis://")
        assert s.cortex_env == "production"
        assert s.litellm_base_url == "http://ollama.perwnet.com:4000"

    def test_invalid_database_url_raises(self) -> None:
        with pytest.raises(ValidationError, match="DATABASE_URL must be"):
            self._make(database_url="postgres://wrong:pw@host:5432/db")

    def test_invalid_redis_url_raises(self) -> None:
        with pytest.raises(ValidationError, match="REDIS_URL must start"):
            self._make(redis_url="rediss://wrong")

    def test_litellm_base_url_default(self) -> None:
        s = self._make()
        assert "ollama.perwnet.com" in s.litellm_base_url
        assert ":4000" in s.litellm_base_url

    def test_sqlite_url_allowed(self) -> None:
        """SQLite is allowed for local dev / CI without MariaDB."""
        s = self._make(database_url="sqlite:///./cortex.db")
        assert s.database_url.startswith("sqlite")
