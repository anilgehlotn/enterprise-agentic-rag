"""Writing analytics: one query_log row per answered request, and feedback votes.

Query logging must never break or slow a user request, so the request thread only
builds a small dict and puts it on a bounded in-memory queue (put_nowait: never waits).
A background thread owns every database write. If the queue is full or the database
is down, the row is dropped and a warning is logged; the user still gets the answer.
While the database is unreachable the writer stops trying for RETRY_COOLDOWN_SECONDS,
so an outage costs one failed connect per minute, not one per request.

Feedback (POST /feedback) is written synchronously: the caller is waiting for "201
recorded" or "404 unknown query", and it is not on the answer path.
"""

import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import logfire

from app.analytics.config import analytics_enabled, store_questions
from app.analytics.db import AnalyticsUnavailable, get_engine, utc_now

QUEUE_MAX_ROWS = 1_000
RETRY_COOLDOWN_SECONDS = 60
MAX_COMMENT_CHARS = 1_000

INTENTS = ("technical", "conversational", "blocked", "error")
SOURCES = ("api", "stream", "mcp")


# ── What gets logged ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueryRecord:
    id: str
    created_at: str
    thread_id: str | None
    source: str
    question: str | None
    intent: str
    retrieval_attempts: int
    grounded: bool | None
    status: str | None
    latency_ms: int
    source_count: int


def derive_intent(result: dict[str, Any]) -> str:
    """
    technical | conversational | blocked | error, from the response the user got.
    The planner writes "Intent: Technical" / "Intent: Conversational/Memory" and the
    guardrails gate writes "Intent: Guardrails Fired" into thought_process.
    """
    if result.get("status") == "error":
        return "error"
    plan = " ".join(result.get("thought_process") or []).lower()
    if "guardrails fired" in plan or result.get("status") == "Blocked by guardrails.":
        return "blocked"
    if "intent: conversational" in plan:
        return "conversational"
    if "intent: technical" in plan or "retrieval attempt" in plan:
        return "technical"
    return "error"  # a response without a recognised plan is a pipeline bug worth seeing


def new_query_id() -> str | None:
    """A fresh query id, or None when analytics is off (the response then carries query_id=null)."""
    return str(uuid.uuid4()) if analytics_enabled() else None


def build_record(
    query_id: str,
    source: str,
    question: str,
    thread_id: str | None,
    result: dict[str, Any],
    retrieval_attempts: int,
    latency_ms: float,
) -> QueryRecord:
    """The row to store. Never the answer, the source content or any key; question only if allowed."""
    grounded = result.get("grounded")
    return QueryRecord(
        id=query_id,
        created_at=utc_now(),
        thread_id=thread_id,
        source=source,
        question=question if store_questions() else None,
        intent=derive_intent(result),
        retrieval_attempts=max(int(retrieval_attempts or 0), 0),
        grounded=grounded if isinstance(grounded, bool) else None,
        status=result.get("status"),
        latency_ms=max(int(round(latency_ms)), 0),
        source_count=len(result.get("sources") or []),
    )


INSERT_QUERY_LOG = """
INSERT INTO query_log (
    id, created_at, thread_id, source, question, intent,
    retrieval_attempts, grounded, status, latency_ms, source_count
) VALUES (
    :id, :created_at, :thread_id, :source, :question, :intent,
    :retrieval_attempts, :grounded, :status, :latency_ms, :source_count
)
"""


def insert_query_log(conn, record: QueryRecord) -> None:
    """One INSERT with bind parameters: the question is data, never part of the SQL text."""
    from sqlalchemy import text

    conn.execute(text(INSERT_QUERY_LOG), asdict(record))


# ── Background writer ─────────────────────────────────────────────────────────


class AnalyticsWriter:
    """Bounded queue + one daemon thread that performs every query_log INSERT."""

    def __init__(self, max_rows: int = QUEUE_MAX_ROWS):
        self._queue: queue.Queue = queue.Queue(maxsize=max_rows)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._retry_at = 0.0
        self.dropped = 0

    def submit(self, record: QueryRecord) -> bool:
        """Never blocks. False (and a warning) when the queue is full."""
        self._ensure_thread()
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped += 1
            logfire.warning("Analytics queue full; query_log row dropped")
            return False

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="analytics-writer", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            record = self._queue.get()
            try:
                if record is None:  # stop signal
                    return
                self._write(record)
            except Exception as exc:  # never let one bad row kill the thread
                self.dropped += 1
                logfire.error(f"Analytics write failed: {type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()

    def _write(self, record: QueryRecord) -> None:
        if time.monotonic() < self._retry_at:
            self.dropped += 1
            return
        try:
            engine = get_engine()
        except AnalyticsUnavailable as exc:
            self._retry_at = time.monotonic() + RETRY_COOLDOWN_SECONDS
            self.dropped += 1
            logfire.warning(f"{exc} Query logging paused for {RETRY_COOLDOWN_SECONDS}s.")
            return
        with engine.begin() as conn:
            insert_query_log(conn, record)

    def flush(self, timeout: float = 2.0) -> bool:
        """Waits until every queued row was handled (written or dropped). False on timeout."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def stop(self, timeout: float = 2.0) -> None:
        """Writes what is queued (up to timeout), then stops the thread."""
        with self._lock:
            thread, self._thread = self._thread, None
        self._retry_at = 0.0
        if thread is None or not thread.is_alive():
            return
        self.flush(timeout)
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            return
        thread.join(timeout)


writer = AnalyticsWriter()


def log_query(
    query_id: str | None,
    source: str,
    question: str,
    thread_id: str | None,
    result: dict[str, Any],
    retrieval_attempts: int,
    latency_ms: float,
) -> None:
    """Queues one query_log row. Never raises and never touches the database on this thread."""
    if query_id is None:
        return
    try:
        writer.submit(build_record(query_id, source, question, thread_id, result, retrieval_attempts, latency_ms))
    except Exception as exc:
        logfire.error(f"Analytics logging skipped: {type(exc).__name__}: {exc}")


# ── Feedback ──────────────────────────────────────────────────────────────────

QUERY_EXISTS = "SELECT 1 FROM query_log WHERE id = :query_id"

# Portable upsert (SQLite >= 3.24 and PostgreSQL >= 9.5 share this syntax). UNIQUE (query_id)
# is the conflict target: a second vote on the same answer updates the row instead of
# adding one; created_at keeps the first vote's time.
UPSERT_FEEDBACK = """
INSERT INTO feedback (id, query_id, rating, comment, created_at, updated_at)
VALUES (:id, :query_id, :rating, :comment, :now, :now)
ON CONFLICT (query_id) DO UPDATE SET
    rating = excluded.rating,
    comment = excluded.comment,
    updated_at = excluded.updated_at
"""

SELECT_FEEDBACK = """
SELECT query_id, rating, comment, created_at, updated_at
FROM feedback
WHERE query_id = :query_id
"""


def _query_exists(engine, query_id: str) -> bool:
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text(QUERY_EXISTS), {"query_id": query_id}).first() is not None


def record_feedback(query_id: str, rating: int, comment: str | None = None) -> dict[str, Any] | None:
    """
    Creates or updates the vote on query_id. Returns the stored feedback, or None when
    query_id is unknown. Raises AnalyticsUnavailable when analytics is off or unreachable.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    if not analytics_enabled():
        raise AnalyticsUnavailable("Analytics is disabled (ANALYTICS_ENABLED=false).")
    if rating not in (-1, 1):
        raise ValueError("rating must be 1 or -1")
    if comment is not None and len(comment) > MAX_COMMENT_CHARS:
        raise ValueError(f"comment is longer than {MAX_COMMENT_CHARS} characters")

    engine = get_engine()
    if not _query_exists(engine, query_id):
        # The answer's row may still be on the writer queue (a very fast click).
        writer.flush(timeout=2.0)
        if not _query_exists(engine, query_id):
            return None

    params = {"id": str(uuid.uuid4()), "query_id": query_id, "rating": rating, "comment": comment, "now": utc_now()}
    try:
        with engine.begin() as conn:
            conn.execute(text(UPSERT_FEEDBACK), params)
            row = conn.execute(text(SELECT_FEEDBACK), {"query_id": query_id}).mappings().one()
    except IntegrityError:  # the FOREIGN KEY backstop: query_id vanished meanwhile
        return None
    return dict(row)
