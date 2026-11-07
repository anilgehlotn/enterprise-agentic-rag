-- One row per answered request (/query, /query/stream, MCP ask_knowledge_base).
-- Portable SQL: runs unchanged on SQLite (local) and PostgreSQL (Cloud Run).
--
-- created_at is TEXT in a fixed-width UTC format, 'YYYY-MM-DD HH:MM:SS.ffffff':
--   * string order == time order, so "created_at >= :since" works on both databases;
--   * SUBSTR(created_at, 1, 10) is the calendar day on both, so GROUP BY day is portable
--     (SQLite has no DATE type and Postgres has no SQLite date() semantics on text).
-- No answers, no source content and no API keys are stored. question is NULL when
-- ANALYTICS_STORE_QUESTIONS=false.

CREATE TABLE IF NOT EXISTS query_log (
    id                 TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    thread_id          TEXT,
    source             TEXT NOT NULL CHECK (source IN ('api', 'stream', 'mcp')),
    question           TEXT,
    intent             TEXT NOT NULL CHECK (intent IN ('technical', 'conversational', 'blocked', 'error')),
    retrieval_attempts INTEGER NOT NULL DEFAULT 0 CHECK (retrieval_attempts >= 0),
    grounded           BOOLEAN,
    status             TEXT,
    latency_ms         INTEGER NOT NULL CHECK (latency_ms >= 0),
    source_count       INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0)
);

-- Every analytics query filters on a time window: created_at >= :since.
CREATE INDEX IF NOT EXISTS idx_query_log_created_at ON query_log (created_at);
