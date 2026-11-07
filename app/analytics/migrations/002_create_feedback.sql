-- Thumbs up / down on an answer. At most one row per query: a second vote updates it
-- (INSERT ... ON CONFLICT (query_id) DO UPDATE, see app/analytics/store.py).

CREATE TABLE IF NOT EXISTS feedback (
    id         TEXT PRIMARY KEY,
    query_id   TEXT NOT NULL UNIQUE REFERENCES query_log (id) ON DELETE CASCADE,
    rating     INTEGER NOT NULL CHECK (rating IN (-1, 1)),
    comment    TEXT CHECK (comment IS NULL OR LENGTH(comment) <= 1000),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The JOIN in the feedback summary looks feedback rows up by query_id. UNIQUE already
-- creates an index on both databases; this named one makes the intent explicit.
CREATE INDEX IF NOT EXISTS idx_feedback_query_id ON feedback (query_id);
