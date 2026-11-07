"""Analytics configuration (pure: no database, no network).

ANALYTICS_ENABLED          true (default) | false  — log queries and accept feedback
ANALYTICS_DATABASE_URL     SQLAlchemy URL; default sqlite file <project root>/state/analytics.sqlite.
                           On Cloud Run point it at Postgres (it may be the same database as DATABASE_URL).
ANALYTICS_STORE_QUESTIONS  true (default) | false  — false stores NULL instead of the question text
ANALYTICS_API_KEY          key for GET /analytics (X-API-Key header). Unset → the endpoint returns 404.
"""

import os
from pathlib import Path

# Same root resolution as the checkpointer: MCP clients start the server from an
# arbitrary working directory, so the default file must not depend on it.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "state" / "analytics.sqlite"

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"Invalid {name}={raw!r}: expected true or false.")


def analytics_enabled() -> bool:
    return _flag("ANALYTICS_ENABLED", True)


def store_questions() -> bool:
    return _flag("ANALYTICS_STORE_QUESTIONS", True)


def analytics_api_key() -> str | None:
    """The configured key, or None when GET /analytics is disabled."""
    key = os.getenv("ANALYTICS_API_KEY", "").strip()
    return key or None


def database_url() -> str:
    """
    SQLAlchemy URL for the analytics database.

    postgres:// and postgresql:// (the form Neon / Supabase / DATABASE_URL use) are
    rewritten to postgresql+psycopg:// so SQLAlchemy uses psycopg 3, the driver already
    installed for the Postgres checkpointer (plain postgresql:// would mean psycopg2).
    """
    url = os.getenv("ANALYTICS_DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{DEFAULT_SQLITE_PATH}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def validate_analytics_config() -> None:
    """Fail fast at startup on a mistyped flag (the database itself is checked lazily)."""
    analytics_enabled()
    store_questions()
