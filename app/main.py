"""ASGI entrypoint.

Run with:  uvicorn app.main:app --reload

Builds the six-agent app with SQLite persistence enabled. The database URL comes
from settings (``DATABASE_URL``); tables are created on startup.
"""

from __future__ import annotations

from app.api.factory import build_app
from app.config import get_settings

settings = get_settings()
app = build_app(settings, database_url=settings.database_url)
