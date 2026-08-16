"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Ensure each test gets fresh settings if it patched the environment."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
