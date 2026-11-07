"""Answer feedback and query analytics, in hand-written SQL (SQLite locally, Postgres on Cloud Run).

    config.py      ANALYTICS_* environment variables
    db.py          SQLAlchemy engine (connection layer only) + SQL migrations
    migrations/    001_create_query_log.sql, 002_create_feedback.sql
    store.py       query logging (background writer) and the feedback upsert
    queries.py     the analytics queries behind GET /analytics

Importing this package opens no database: the engine is created on first use.
"""

from app.analytics.config import analytics_api_key, analytics_enabled, store_questions, validate_analytics_config
from app.analytics.db import AnalyticsUnavailable, dispose_engine, get_engine
from app.analytics.store import log_query, new_query_id, record_feedback, writer

__all__ = [
    "AnalyticsUnavailable",
    "analytics_api_key",
    "analytics_enabled",
    "get_summary",
    "init_analytics",
    "log_query",
    "new_query_id",
    "record_feedback",
    "shutdown_analytics",
    "validate_analytics_config",
]


def init_analytics() -> bool:
    """
    Startup: create the engine and apply migrations, so a broken database shows up in
    the logs at deploy time. A database failure is logged, never raised: analytics is
    optional, answering is not. (Invalid ANALYTICS_* flags are rejected earlier by
    validate_analytics_config(), like every other setting.)
    """
    if not analytics_enabled():
        return False
    try:
        get_engine()
        return True
    except AnalyticsUnavailable as exc:
        import logfire

        logfire.error(f"{exc} Query logging and feedback are unavailable until it recovers.")
        return False


def get_summary(days: int) -> dict:
    """The GET /analytics payload for the last `days` days. Raises AnalyticsUnavailable."""
    from app.analytics.queries import analytics_summary

    if not analytics_enabled():
        raise AnalyticsUnavailable("Analytics is disabled (ANALYTICS_ENABLED=false).")
    writer.flush(timeout=1.0)  # include rows still on the queue
    return analytics_summary(get_engine(), days, include_questions=store_questions())


def shutdown_analytics(timeout: float = 2.0) -> None:
    """Writes the queued rows (up to timeout), stops the writer thread and closes the pool."""
    writer.stop(timeout)
    dispose_engine()
