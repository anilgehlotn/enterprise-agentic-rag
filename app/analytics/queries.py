"""The analytics queries behind GET /analytics: hand-written SQL, one named function each.

Every function takes an open connection and `since` (the start of the time window, in
the stored created_at format) and passes all values as named bind parameters (:since,
:min_count, ...). No value is ever formatted into the SQL text, so a question such as
"'; DROP TABLE query_log; --" is compared and returned as plain data.

Portability (the same SQL runs on SQLite and PostgreSQL):
  * Day grouping: created_at is fixed-width UTC text 'YYYY-MM-DD HH:MM:SS.ffffff', so
    SUBSTR(created_at, 1, 10) is the calendar day on both databases. SQLite's date() and
    Postgres's DATE_TRUNC() / ::date are not shared, and SQLite has no date type at all.
  * Window filter: created_at >= :since compares strings, which for this format is the
    same as comparing times.
  * AVG over integers returns a float on SQLite and a Decimal on Postgres; _num() turns
    both into float. Division uses NULLIF(denominator, 0): SQLite returns NULL for x/0,
    Postgres raises an error, and NULLIF makes both return NULL.
  * p95 latency is computed in Python (latency_percentiles): Postgres has
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), SQLite has no percentile
    function, so the SQL only fetches the latencies.
"""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

TOP_QUESTIONS_LIMIT = 10
TOP_QUESTIONS_MIN_COUNT = 2


def window_start(days: int, now: datetime | None = None) -> str:
    """Midnight UTC `days - 1` days ago: days=7 → today plus the six full days before it."""
    now = now or datetime.now(UTC)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%d %H:%M:%S.%f")


def _num(value, digits: int = 4) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    return round(float(value), digits)


def _rows(conn, sql: str, params: dict) -> list[dict[str, Any]]:
    from sqlalchemy import text

    return [dict(row) for row in conn.execute(text(sql), params).mappings()]


# ── a) queries per day ────────────────────────────────────────────────────────

QUERIES_PER_DAY = """
SELECT SUBSTR(created_at, 1, 10) AS day,
       COUNT(*)                  AS queries
FROM query_log
WHERE created_at >= :since
GROUP BY SUBSTR(created_at, 1, 10)
ORDER BY day
"""


def queries_per_day(conn, since: str) -> list[dict[str, Any]]:
    """[{day: 'YYYY-MM-DD', queries: n}] for days with at least one query, oldest first."""
    return [{"day": r["day"], "queries": int(r["queries"])} for r in _rows(conn, QUERIES_PER_DAY, {"since": since})]


# ── b) intent breakdown ───────────────────────────────────────────────────────

INTENT_BREAKDOWN = """
SELECT intent,
       COUNT(*) AS queries
FROM query_log
WHERE created_at >= :since
GROUP BY intent
ORDER BY queries DESC, intent
"""


def intent_breakdown(conn, since: str) -> list[dict[str, Any]]:
    rows = _rows(conn, INTENT_BREAKDOWN, {"since": since})
    return [{"intent": r["intent"], "queries": int(r["queries"])} for r in rows]


# ── c) grounded rate of technical answers ─────────────────────────────────────

# CASE without ELSE yields NULL for unverified answers (grounded IS NULL), and AVG
# skips NULLs, so grounded_rate = grounded / verified, not grounded / all.
GROUNDED_RATE = """
SELECT COUNT(*)                                             AS technical_answers,
       COUNT(grounded)                                      AS verified_answers,
       SUM(CASE WHEN grounded = TRUE THEN 1 ELSE 0 END)     AS grounded_answers,
       AVG(CASE WHEN grounded = TRUE  THEN 1.0
                WHEN grounded = FALSE THEN 0.0 END)          AS grounded_rate
FROM query_log
WHERE created_at >= :since
  AND intent = 'technical'
"""


def grounded_rate(conn, since: str) -> dict[str, Any]:
    (row,) = _rows(conn, GROUNDED_RATE, {"since": since})
    return {
        "technical_answers": int(row["technical_answers"]),
        "verified_answers": int(row["verified_answers"]),
        "grounded_answers": int(row["grounded_answers"] or 0),
        "grounded_rate": _num(row["grounded_rate"]),
    }


# ── d) retrieval attempts and rewrites ────────────────────────────────────────

# attempts > 1 means the grader found nothing relevant, the rewriter rephrased the
# query and the retriever ran again (Corrective RAG).
RETRIEVAL_STATS = """
SELECT COUNT(*)                                                AS technical_questions,
       AVG(retrieval_attempts * 1.0)                           AS avg_retrieval_attempts,
       SUM(CASE WHEN retrieval_attempts > 1 THEN 1 ELSE 0 END) AS rewritten_questions,
       AVG(CASE WHEN retrieval_attempts > 1 THEN 1.0 ELSE 0.0 END) AS rewrite_rate
FROM query_log
WHERE created_at >= :since
  AND intent = 'technical'
"""


def retrieval_stats(conn, since: str) -> dict[str, Any]:
    (row,) = _rows(conn, RETRIEVAL_STATS, {"since": since})
    return {
        "technical_questions": int(row["technical_questions"]),
        "avg_retrieval_attempts": _num(row["avg_retrieval_attempts"]),
        "rewritten_questions": int(row["rewritten_questions"] or 0),
        "rewrite_rate": _num(row["rewrite_rate"]),
    }


# ── e) feedback summary (JOIN) ────────────────────────────────────────────────

# LEFT JOIN, not INNER JOIN: every query in the window must stay in the result, with
# NULL feedback columns when nobody voted. An INNER JOIN would keep only the voted
# queries, and "share of answers that received feedback" would always be 100%.
# COUNT(f.id) counts only matched rows (NULLs are skipped); COUNT(*) counts all queries.
FEEDBACK_SUMMARY = """
SELECT COUNT(*)                                          AS answers,
       COUNT(f.id)                                       AS answers_with_feedback,
       SUM(CASE WHEN f.rating = 1  THEN 1 ELSE 0 END)    AS thumbs_up,
       SUM(CASE WHEN f.rating = -1 THEN 1 ELSE 0 END)    AS thumbs_down,
       AVG(CASE WHEN f.rating = 1  THEN 1.0
                WHEN f.rating = -1 THEN 0.0 END)         AS satisfaction_rate,
       COUNT(f.id) * 1.0 / NULLIF(COUNT(*), 0)           AS feedback_rate
FROM query_log AS q
LEFT JOIN feedback AS f
       ON f.query_id = q.id
WHERE q.created_at >= :since
"""


def feedback_summary(conn, since: str) -> dict[str, Any]:
    (row,) = _rows(conn, FEEDBACK_SUMMARY, {"since": since})
    return {
        "answers": int(row["answers"]),
        "answers_with_feedback": int(row["answers_with_feedback"]),
        "thumbs_up": int(row["thumbs_up"] or 0),
        "thumbs_down": int(row["thumbs_down"] or 0),
        "satisfaction_rate": _num(row["satisfaction_rate"]),
        "feedback_rate": _num(row["feedback_rate"]),
    }


# ── f) most frequent questions ────────────────────────────────────────────────

# WHERE filters rows before grouping; HAVING filters the groups after COUNT(*) is known.
# LOWER/TRIM make "What is a Job? " and "what is a job?" one question (SQLite's LOWER
# only folds ASCII letters).
TOP_QUESTIONS = """
SELECT LOWER(TRIM(question)) AS normalized_question,
       COUNT(*)              AS times_asked
FROM query_log
WHERE created_at >= :since
  AND question IS NOT NULL
GROUP BY LOWER(TRIM(question))
HAVING COUNT(*) >= :min_count
ORDER BY times_asked DESC, normalized_question
LIMIT :limit
"""


def top_questions(
    conn, since: str, limit: int = TOP_QUESTIONS_LIMIT, min_count: int = TOP_QUESTIONS_MIN_COUNT
) -> list[dict[str, Any]]:
    rows = _rows(conn, TOP_QUESTIONS, {"since": since, "min_count": min_count, "limit": limit})
    return [{"question": r["normalized_question"], "times_asked": int(r["times_asked"])} for r in rows]


# ── g) latency per source ─────────────────────────────────────────────────────

LATENCY_BY_SOURCE = """
SELECT source,
       COUNT(*)        AS queries,
       AVG(latency_ms) AS avg_latency_ms,
       MAX(latency_ms) AS max_latency_ms
FROM query_log
WHERE created_at >= :since
GROUP BY source
ORDER BY source
"""

# Raw values for the p95, computed in Python (see the module docstring).
LATENCIES_BY_SOURCE = """
SELECT source, latency_ms
FROM query_log
WHERE created_at >= :since
ORDER BY source, latency_ms
"""


def percentile(sorted_values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of an ascending list: the smallest value with >= fraction of values at or below it."""
    if not sorted_values:
        return None
    rank = max(math.ceil(fraction * len(sorted_values)), 1)
    return float(sorted_values[rank - 1])


def latency_by_source(conn, since: str) -> list[dict[str, Any]]:
    latencies: dict[str, list[int]] = {}
    for row in _rows(conn, LATENCIES_BY_SOURCE, {"since": since}):
        latencies.setdefault(row["source"], []).append(int(row["latency_ms"]))
    return [
        {
            "source": r["source"],
            "queries": int(r["queries"]),
            "avg_latency_ms": _num(r["avg_latency_ms"], 1),
            "max_latency_ms": int(r["max_latency_ms"]),
            "p95_latency_ms": percentile(latencies.get(r["source"], []), 0.95),
        }
        for r in _rows(conn, LATENCY_BY_SOURCE, {"since": since})
    ]


# ── Everything for GET /analytics ─────────────────────────────────────────────


def analytics_summary(engine, days: int, include_questions: bool, now: datetime | None = None) -> dict[str, Any]:
    """Runs every query above in one read transaction and returns the /analytics JSON."""
    since = window_start(days, now)
    with engine.connect() as conn:
        per_day = queries_per_day(conn, since)
        return {
            "days": days,
            "since": since,
            "total_queries": sum(day["queries"] for day in per_day),
            "queries_per_day": per_day,
            "intents": intent_breakdown(conn, since),
            "grounded": grounded_rate(conn, since),
            "retrieval": retrieval_stats(conn, since),
            "feedback": feedback_summary(conn, since),
            "questions_stored": include_questions,
            "top_questions": top_questions(conn, since) if include_questions else [],
            "latency": latency_by_source(conn, since),
        }
