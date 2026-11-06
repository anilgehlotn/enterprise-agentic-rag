"""Checkpointer factory: where LangGraph keeps conversation state between requests.

A checkpointer saves the graph state (messages, plan, documents, ...) after every
step, keyed by thread_id. The next request with the same thread_id resumes from the
latest checkpoint, which is how follow-up questions see earlier turns.

CHECKPOINTER selects the backend:
  memory    in-process dict (MemorySaver). Lost on restart; fine for tests.
  sqlite    (default) a local file, SQLITE_CHECKPOINT_PATH (default <project root>/state/checkpoints.sqlite).
            Survives restarts on one machine. NOT for Cloud Run: the container disk is
            in-memory, wiped on every new instance, and not shared between instances.
  postgres  DATABASE_URL (Neon / Supabase / Cloud SQL). Shared by every Cloud Run
            instance and survives redeploys.

One checkpointer is opened per process (open_checkpointer / get_checkpointer) and
closed on API shutdown (close_checkpointer). Backend packages are imported lazily, so
`memory` needs neither langgraph-checkpoint-sqlite nor psycopg.
"""

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import logfire

BACKENDS = ("memory", "sqlite", "postgres")
DEFAULT_BACKEND = "sqlite"
# Not ./data/: on case-insensitive filesystems (macOS, Windows) that IS the DATA/ corpus
# folder, so the checkpoint DB would land among the documents being ingested.
DEFAULT_SQLITE_PATH = "./state/checkpoints.sqlite"
# The default is resolved from the project root, not the working directory: MCP clients
# such as Claude Desktop start the server from an arbitrary (possibly read-only) folder.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Small pool: max 2 Cloud Run instances x a handful of concurrent requests each.
# Free Neon / Supabase tiers allow only a few dozen connections in total.
POSTGRES_POOL_MAX_SIZE = 5


@dataclass
class CheckpointerHandle:
    """A checkpointer plus the function that releases its connection."""

    backend: str
    saver: Any
    close: Callable[[], None]


def resolve_checkpointer_backend(value: str | None = None) -> str:
    """Reads CHECKPOINTER (default sqlite). Raises ValueError on an unknown value."""
    if value is None:
        value = os.getenv("CHECKPOINTER", DEFAULT_BACKEND)
    backend = (value or DEFAULT_BACKEND).strip().lower()
    if backend not in BACKENDS:
        raise ValueError(f"Invalid CHECKPOINTER={value!r}. Use one of: {', '.join(BACKENDS)}.")
    return backend


def sqlite_checkpoint_path() -> Path:
    """SQLITE_CHECKPOINT_PATH as given (relative = from the working directory), else <project root>/state/."""
    configured = os.getenv("SQLITE_CHECKPOINT_PATH")
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / DEFAULT_SQLITE_PATH


def _memory() -> CheckpointerHandle:
    from langgraph.checkpoint.memory import MemorySaver

    return CheckpointerHandle("memory", MemorySaver(), lambda: None)


def _sqlite() -> CheckpointerHandle:
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    path = sqlite_checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI runs sync endpoints in a thread pool. Safe here
    # because SqliteSaver serialises access to the connection with its own lock.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()  # create tables now, so a broken path fails at startup, not on the first query
    logfire.info(f"Checkpointer: sqlite at {path}")
    return CheckpointerHandle("sqlite", saver, conn.close)


def _postgres() -> CheckpointerHandle:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise ValueError("CHECKPOINTER=postgres needs DATABASE_URL (e.g. postgresql://user:pass@host/db).")

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # A pool rather than one connection: concurrent requests don't queue behind one
    # socket, and check_connection replaces connections the server closed while idle
    # (Neon suspends idle databases). The kwargs are the ones PostgresSaver requires:
    # autocommit (setup() and writes commit immediately), dict rows, no prepared
    # statements (they break behind PgBouncer-style poolers such as Supabase's).
    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=POSTGRES_POOL_MAX_SIZE,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=ConnectionPool.check_connection,
        open=True,
    )
    try:
        saver = PostgresSaver(pool)
        saver.setup()  # creates/migrates the checkpoint tables; idempotent
    except Exception:
        pool.close()
        raise
    logfire.info("Checkpointer: postgres (DATABASE_URL)")
    return CheckpointerHandle("postgres", saver, pool.close)


_FACTORIES = {"memory": _memory, "sqlite": _sqlite, "postgres": _postgres}


def build_checkpointer(backend: str | None = None) -> CheckpointerHandle:
    """Creates a new checkpointer for `backend` (default: the CHECKPOINTER env var)."""
    return _FACTORIES[resolve_checkpointer_backend(backend)]()


# ── Process-wide checkpointer ─────────────────────────────────────────────────

_handle: CheckpointerHandle | None = None
_lock = threading.Lock()


def open_checkpointer() -> CheckpointerHandle:
    """Opens the process checkpointer once (idempotent, thread-safe) and returns it."""
    global _handle
    with _lock:
        if _handle is None:
            _handle = build_checkpointer()
        return _handle


def get_checkpointer():
    """The process checkpointer's saver, opened on first use (e.g. by the MCP server)."""
    return open_checkpointer().saver


def close_checkpointer() -> None:
    """Closes the process checkpointer's connection. The next get_checkpointer() reopens it."""
    global _handle
    with _lock:
        handle, _handle = _handle, None
    if handle is not None:
        handle.close()
        logfire.info(f"Checkpointer closed ({handle.backend})")
