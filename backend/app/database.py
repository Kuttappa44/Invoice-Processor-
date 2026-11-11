"""Engine factory. Same URL and pool_pre_ping as the previous hardcoded helpers."""

from __future__ import annotations

import urllib.parse

from sqlalchemy import create_engine

from backend.app.config import Settings, get_settings


def engine_url(settings: Settings | None = None) -> str:
    """Build the SQLAlchemy URL. Kept as a string so tests need no MySQL driver."""
    settings = settings or get_settings()
    encoded_password = urllib.parse.quote_plus(settings.mysql_password)
    return (
        f"mysql+mysqlconnector://{settings.mysql_user}:{encoded_password}"
        f"@{settings.mysql_host}/{settings.mysql_database}"
    )


def get_engine():
    return create_engine(engine_url(), pool_pre_ping=True)
