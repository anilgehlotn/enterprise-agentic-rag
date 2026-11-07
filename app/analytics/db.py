"""Analytics database: engine (SQLAlchemy Core as a connection layer only) and SQL migrations.

Every statement in app/analytics is hand-written SQL run through sqlalchemy.text() with
named bind parameters (:name). SQLAlchemy only provides the connection pool and the
driver differences (sqlite3 locally, psycopg 3 on Postgres), so the same SQL runs on both.

Migrations: app/analytics/migrations/NNN_name.sql, applied in file-name order. Each
applied version is recorded in schema_migrations, so start-up applies only new files.
The DDL also uses IF NOT EXISTS and the version insert uses ON CONFLICT DO NOTHING, so
two Cloud Run instances starting at the same time can't fail on each other's work.
"""

import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import logfire

from app.analytics.config import database_url

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_FILE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

# Small pool: 2 Cloud Run instances share a free-tier Postgres with the checkpointer.
POOL_SIZE, MAX_OVERFLOW = 2, 2
CONNECT_TIMEOUT_SECONDS = 5


def utc_now() -> str:
    """The stored timestamp format: fixed-width UTC text, so string order == time order."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def create_analytics_engine(url: str | None = None):
    """A new SQLAlchemy engine for url (default: ANALYTICS_DATABASE_URL). Creates the SQLite folder."""
    from sqlalchemy import create_engine, event

    url = url or database_url()
    if url.startswith("sqlite"):
        path = url.split("sqlite:///", 1)[-1]
        if path and path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # timeout = how long a writer waits for SQLite's file lock instead of failing at once.
        engine = create_engine(url, connect_args={"timeout": CONNECT_TIMEOUT_SECONDS})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            # SQLite ignores FOREIGN KEY constraints unless this is on, per connection.
            cursor.execute("PRAGMA foreign_keys = ON")
            # WAL: readers (GET /analytics) don't block the writer thread and vice versa.
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.close()

        return engine

    return create_engine(
        url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,  # Neon suspends idle databases; replace dead connections silently
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    """(version, path) for every NNN_name.sql file, sorted by version."""
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE.match(path.name)
        if not match:
            raise ValueError(f"Badly named migration file {path.name}: expected NNN_lowercase_name.sql")
        found.append((match.group(1), path))
    versions = [version for version, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"Duplicate migration versions in {directory}")
    return found


def split_statements(sql: str) -> list[str]:
    """
    The statements of a migration file. `--` comments are removed first (they may contain
    semicolons), then the text is split on ';'. Migration files must not put ';' or '--'
    inside string literals; ours don't.
    """
    without_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [statement.strip() for statement in without_comments.split(";") if statement.strip()]


def apply_migrations(engine, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Applies every migration not yet in schema_migrations, in order. Returns the versions applied now."""
    from sqlalchemy import text

    applied_now = []
    with engine.begin() as conn:
        conn.execute(text(CREATE_SCHEMA_MIGRATIONS))
        done = {row.version for row in conn.execute(text("SELECT version FROM schema_migrations"))}

    for version, path in migration_files(directory):
        if version in done:
            continue
        # One transaction per migration: the DDL and its schema_migrations row commit together.
        with engine.begin() as conn:
            for statement in split_statements(path.read_text(encoding="utf-8")):
                conn.execute(text(statement))
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (:version, :applied_at) "
                    "ON CONFLICT (version) DO NOTHING"
                ),
                {"version": version, "applied_at": utc_now()},
            )
        applied_now.append(version)
        logfire.info(f"Analytics migration {path.name} applied")
    return applied_now


# ── Process-wide engine ───────────────────────────────────────────────────────


class AnalyticsUnavailable(RuntimeError):
    """Analytics is disabled or its database can't be reached."""


_engine = None
_lock = threading.Lock()


def get_engine():
    """The process engine, created and migrated on first use. Raises AnalyticsUnavailable on failure."""
    global _engine
    with _lock:
        if _engine is None:
            engine = None
            try:
                engine = create_analytics_engine()
                apply_migrations(engine)
            except Exception as exc:
                if engine is not None:
                    engine.dispose()
                raise AnalyticsUnavailable(f"Analytics database unavailable ({type(exc).__name__}).") from exc
            _engine = engine
        return _engine


def dispose_engine() -> None:
    global _engine
    with _lock:
        engine, _engine = _engine, None
    if engine is not None:
        engine.dispose()
