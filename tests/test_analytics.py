"""Query analytics + feedback: SQL migrations, logging, queries, endpoints. Temporary SQLite, no network."""

import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import analytics
from app.agents import graph as graph_module
from app.analytics import db, queries, store
from app.services import rag_service

TECHNICAL_OUTPUT = {
    "final_answer": "Jobs run Pods to completion [1].",
    "plan": [
        "Intent: Technical",
        "Search Term: job",
        "Retrieval attempt 1/2 (hybrid): 3 chunks retrieved",
        "Verification: answer supported by sources ✅",
    ],
    "status": "Response generated.",
    "documents": [{"source": "jobs.html", "source_type": "true", "content": "A Job runs Pods.", "score": 0.8}],
    "grounded": True,
    "retrieval_attempts": 1,
}


# ── Helpers ───────────────────────────────────────────────────────────────────


class FakeAgent:
    def __init__(self, output=None, error=None, updates=None):
        self.output, self.error, self.updates = output, error, updates or []

    def invoke(self, state, config):
        if self.error:
            raise self.error
        return self.output

    def stream(self, state, config, stream_mode):
        if self.error:
            raise self.error
        for node, update in self.updates:
            yield "updates", {node: update}


def agent_factory(agent):
    """Stands in for graph.get_rag_agent, including the cache_clear() the API's shutdown calls."""

    def get_rag_agent():
        return agent

    get_rag_agent.cache_clear = lambda: None
    return get_rag_agent


@pytest.fixture
def guardrails(monkeypatch):
    fake = SimpleNamespace(verdict=(False, None), ensure_rails=lambda: None)
    fake.guard = lambda message: fake.verdict
    monkeypatch.setitem(sys.modules, "app.guardrails", fake)
    return fake


@pytest.fixture
def use_agent(monkeypatch):
    def install(agent):
        monkeypatch.setattr(graph_module, "get_rag_agent", agent_factory(agent))
        return agent

    return install


def rows(url: str, sql: str = "SELECT * FROM query_log ORDER BY created_at", params=None) -> list[dict]:
    analytics.writer.flush(5)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params or {}).mappings()]
    finally:
        engine.dispose()


def table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


# ── Migrations ────────────────────────────────────────────────────────────────


def test_migrations_apply_once_and_are_idempotent(tmp_path):
    engine = db.create_analytics_engine(f"sqlite:///{tmp_path / 'a.sqlite'}")
    try:
        assert db.apply_migrations(engine) == ["001", "002"]
        assert db.apply_migrations(engine) == []  # second start-up: nothing to do
        with engine.connect() as conn:
            versions = conn.execute(text("SELECT version FROM schema_migrations ORDER BY version")).scalars().all()
            indexes = {r[1] for r in conn.execute(text("PRAGMA index_list('query_log')"))}
            fk = conn.execute(text("PRAGMA foreign_key_list('feedback')")).mappings().one()
        assert versions == ["001", "002"]
        assert "idx_query_log_created_at" in indexes
        assert (fk["table"], fk["from"], fk["to"]) == ("query_log", "query_id", "id")
        assert {"query_log", "feedback", "schema_migrations"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_only_new_migrations_are_applied(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_one.sql").write_text("CREATE TABLE IF NOT EXISTS one (id INTEGER); -- a; comment", "utf-8")
    engine = db.create_analytics_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    try:
        assert db.apply_migrations(engine, migrations) == ["001"]
        (migrations / "002_two.sql").write_text("CREATE TABLE two (id INTEGER);\nCREATE TABLE three (id INTEGER);")
        assert db.apply_migrations(engine, migrations) == ["002"]
        assert {"one", "two", "three"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_split_statements_ignores_semicolons_in_comments():
    sql = "-- first; comment\nCREATE TABLE a (x INT); -- trailing; note\n\nCREATE INDEX i ON a (x);\n"

    assert db.split_statements(sql) == ["CREATE TABLE a (x INT)", "CREATE INDEX i ON a (x)"]


def test_badly_named_migration_is_rejected(tmp_path):
    (tmp_path / "1_bad.sql").write_text("SELECT 1")

    with pytest.raises(ValueError, match="Badly named"):
        db.migration_files(tmp_path)


def test_constraints_reject_bad_rows(analytics_db):
    from sqlalchemy.exc import IntegrityError

    engine = analytics.get_engine()
    good = dict(
        id="q1", created_at=db.utc_now(), thread_id="t", source="api", question="q", intent="technical",
        retrieval_attempts=1, grounded=None, status="ok", latency_ms=5, source_count=0,
    )  # fmt: skip
    with engine.begin() as conn:
        store.insert_query_log(conn, store.QueryRecord(**good))
    bad_rows = [
        {**good, "id": "q2", "source": "cli"},  # CHECK source IN (...)
        {**good, "id": "q3", "intent": "chit-chat"},  # CHECK intent IN (...)
        {**good, "id": "q1"},  # PRIMARY KEY
    ]
    for bad in bad_rows:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            store.insert_query_log(conn, store.QueryRecord(**bad))
    feedback = "INSERT INTO feedback VALUES (:id, :query_id, :rating, NULL, 'now', 'now')"
    for params in ({"id": "f1", "query_id": "missing", "rating": 1}, {"id": "f2", "query_id": "q1", "rating": 5}):
        with pytest.raises(IntegrityError), engine.begin() as conn:  # FOREIGN KEY, CHECK rating
            conn.execute(text(feedback), params)


# ── Database URL / config ────────────────────────────────────────────────────


def test_database_url_defaults_and_postgres_rewrite(monkeypatch):
    monkeypatch.delenv("ANALYTICS_DATABASE_URL", raising=False)
    assert analytics.config.database_url() == f"sqlite:///{analytics.config.PROJECT_ROOT}/state/analytics.sqlite"

    monkeypatch.setenv("ANALYTICS_DATABASE_URL", "postgresql://u:p@host/db?sslmode=require")
    assert analytics.config.database_url() == "postgresql+psycopg://u:p@host/db?sslmode=require"
    monkeypatch.setenv("ANALYTICS_DATABASE_URL", "postgres://u:p@host/db")
    assert analytics.config.database_url() == "postgresql+psycopg://u:p@host/db"


def test_invalid_flag_is_rejected(monkeypatch):
    monkeypatch.setenv("ANALYTICS_STORE_QUESTIONS", "maybe")

    with pytest.raises(ValueError, match="ANALYTICS_STORE_QUESTIONS"):
        analytics.validate_analytics_config()


# ── Logging from the service layer ───────────────────────────────────────────


def test_intent_derivation():
    assert store.derive_intent({"status": "error", "thought_process": ["Intent: Technical"]}) == "error"
    assert store.derive_intent({"status": "Blocked by guardrails.", "thought_process": []}) == "blocked"
    assert store.derive_intent({"thought_process": ["Intent: Guardrails Fired"]}) == "blocked"
    assert store.derive_intent({"thought_process": ["Intent: Conversational/Memory"]}) == "conversational"
    assert store.derive_intent({"thought_process": ["Intent: Technical", "Search Term: memory"]}) == "technical"
    assert store.derive_intent({"thought_process": []}) == "error"


def test_technical_answer_is_logged_without_answer_or_sources(analytics_db, guardrails, use_agent):
    use_agent(FakeAgent(output=TECHNICAL_OUTPUT))

    result = rag_service.answer_question("What is a Job?", "thread-1")

    (row,) = rows(analytics_db)
    assert result["query_id"] == row["id"] and len(row["id"]) == 36
    assert {k: row[k] for k in ("thread_id", "source", "question", "intent", "retrieval_attempts")} == {
        "thread_id": "thread-1",
        "source": "api",
        "question": "What is a Job?",
        "intent": "technical",
        "retrieval_attempts": 1,
    }
    assert row["grounded"] == 1 and row["status"] == "Response generated." and row["source_count"] == 1
    assert row["latency_ms"] >= 0 and len(row["created_at"]) == 26
    assert "Jobs run Pods" not in str(row) and "A Job runs Pods" not in str(row)  # no answer, no source text


def test_ungrounded_answer_after_a_rewrite(analytics_db, guardrails, use_agent):
    use_agent(FakeAgent(output={**TECHNICAL_OUTPUT, "grounded": False, "retrieval_attempts": 2}))

    rag_service.answer_question("What is backoffLimit?", "t", source="mcp")

    (row,) = rows(analytics_db)
    assert (row["source"], row["intent"], row["grounded"], row["retrieval_attempts"]) == ("mcp", "technical", 0, 2)


def test_conversational_blocked_and_error_paths(analytics_db, guardrails, use_agent):
    use_agent(
        FakeAgent(
            output={
                "final_answer": "Hello!",
                "plan": ["Intent: Conversational/Memory", "Retrieval: Skipped"],
                "status": "Response generated.",
                "documents": [],
                "grounded": None,
                "retrieval_attempts": 0,
            }
        )
    )
    rag_service.answer_question("hi there", "t")
    guardrails.verdict = (True, "I only answer technical questions.")
    blocked = rag_service.answer_question("tell me a joke", "t")
    guardrails.verdict = (False, None)
    use_agent(FakeAgent(error=RuntimeError("llm down")))
    error = rag_service.answer_question("What is a Job?", "t")

    logged = rows(analytics_db)
    assert [(r["question"], r["intent"], r["grounded"], r["retrieval_attempts"]) for r in logged] == [
        ("hi there", "conversational", None, 0),
        ("tell me a joke", "blocked", None, 0),
        ("What is a Job?", "error", None, 0),
    ]
    assert blocked["query_id"] == logged[1]["id"] and error["query_id"] == logged[2]["id"]
    assert logged[2]["status"] == "error"


def test_stream_logs_source_stream_and_sends_query_id_in_done(analytics_db, guardrails, use_agent):
    use_agent(
        FakeAgent(
            updates=[
                ("planner", {"plan": ["Intent: Technical", "Search Term: job"], "status": "Planning"}),
                ("retriever", {"retrieval_attempts": 1, "documents": TECHNICAL_OUTPUT["documents"]}),
                ("responder", {"final_answer": "Jobs run Pods.", "status": "Response generated."}),
                ("verifier", {"grounded": True}),
            ]
        )
    )

    events = list(rag_service.stream_answer("What is a Job?", "t"))

    done = events[-1]
    assert done[0] == "done"
    (row,) = rows(analytics_db)
    assert done[1]["query_id"] == row["id"]
    assert (row["source"], row["intent"], row["grounded"], row["retrieval_attempts"]) == ("stream", "technical", 1, 1)


def test_stream_error_and_block_are_logged(analytics_db, guardrails, use_agent):
    use_agent(FakeAgent(error=RuntimeError("boom")))
    assert list(rag_service.stream_answer("q1", "t"))[-1][0] == "error"
    guardrails.verdict = (True, "No.")
    blocked_events = list(rag_service.stream_answer("q2", "t"))

    logged = rows(analytics_db)
    assert [(r["source"], r["intent"]) for r in logged] == [("stream", "error"), ("stream", "blocked")]
    assert blocked_events[-1][1]["query_id"] == logged[1]["id"]


def test_question_is_null_when_storing_questions_is_off(analytics_db, guardrails, use_agent, monkeypatch):
    monkeypatch.setenv("ANALYTICS_STORE_QUESTIONS", "false")
    use_agent(FakeAgent(output=TECHNICAL_OUTPUT))

    rag_service.answer_question("my private question", "t")

    (row,) = rows(analytics_db)
    assert row["question"] is None and row["intent"] == "technical"


def test_query_id_is_null_when_analytics_is_disabled(guardrails, use_agent, monkeypatch):
    monkeypatch.setenv("ANALYTICS_ENABLED", "false")
    use_agent(FakeAgent(output=TECHNICAL_OUTPUT))

    assert rag_service.answer_question("What is a Job?", "t")["query_id"] is None


def test_sql_injection_text_is_stored_as_plain_data(analytics_db, guardrails, use_agent):
    attack = "'; DROP TABLE query_log; --"
    use_agent(FakeAgent(output=TECHNICAL_OUTPUT))

    rag_service.answer_question(attack, "t")
    rag_service.answer_question(attack, "t")

    stored = rows(analytics_db, "SELECT question FROM query_log WHERE question = :q", {"q": attack})
    assert [r["question"] for r in stored] == [attack, attack]
    assert {"query_log", "feedback", "schema_migrations"} <= table_names(analytics_db)
    # Grouped and returned as data too (normalised with LOWER/TRIM like every question).
    summary = analytics.get_summary(1)
    assert summary["top_questions"] == [{"question": attack.lower(), "times_asked": 2}]


# ── Failure isolation ─────────────────────────────────────────────────────────


def test_broken_analytics_database_does_not_break_query(tmp_path, monkeypatch, api_app):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")
    monkeypatch.setenv("ANALYTICS_DATABASE_URL", f"sqlite:///{not_a_dir / 'analytics.sqlite'}")
    monkeypatch.setattr(graph_module, "get_rag_agent", agent_factory(FakeAgent(output=TECHNICAL_OUTPUT)))

    with TestClient(api_app.main.app) as client:  # startup: init_analytics() logs the failure, doesn't raise
        response = client.post("/query", json={"q": "What is a Job?"})
        feedback = client.post("/feedback", json={"query_id": response.json()["query_id"], "rating": 1})

    assert response.status_code == 200
    assert response.json()["answer"] == "Jobs run Pods to completion [1]."
    assert analytics.writer.flush(5)
    assert feedback.status_code == 503


def test_slow_analytics_database_does_not_slow_the_answer(analytics_db, guardrails, use_agent, monkeypatch):
    def slow_engine():
        time.sleep(1.0)
        raise analytics.AnalyticsUnavailable("too slow")

    monkeypatch.setattr(store, "get_engine", slow_engine)
    use_agent(FakeAgent(output=TECHNICAL_OUTPUT))

    started = time.perf_counter()
    result = rag_service.answer_question("What is a Job?", "t")
    elapsed = time.perf_counter() - started

    assert result["answer"] == "Jobs run Pods to completion [1]."
    assert elapsed < 0.5  # the write happens on the background thread


def test_full_queue_drops_rows_instead_of_blocking(monkeypatch):
    small = store.AnalyticsWriter(max_rows=1)
    monkeypatch.setattr(small, "_ensure_thread", lambda: None)  # no consumer: the queue stays full
    record = store.QueryRecord("id", db.utc_now(), None, "api", None, "technical", 0, None, None, 1, 0)

    assert small.submit(record) is True
    assert small.submit(record) is False
    assert small.dropped == 1


def test_log_query_never_raises(monkeypatch):
    monkeypatch.setattr(store, "build_record", lambda *a: (_ for _ in ()).throw(RuntimeError("bad")))

    store.log_query("id", "api", "q", "t", {}, 0, 1.0)  # logged, not raised


# ── POST /feedback ────────────────────────────────────────────────────────────


@pytest.fixture
def client(analytics_db, api_app, monkeypatch):
    monkeypatch.setattr(graph_module, "get_rag_agent", agent_factory(FakeAgent(output=TECHNICAL_OUTPUT)))
    with TestClient(api_app.main.app) as test_client:
        yield test_client


def ask(client) -> str:
    response = client.post("/query", json={"q": "What is a Job?"})
    assert response.status_code == 200
    return response.json()["query_id"]


def test_query_response_shape_only_gains_query_id(client):
    body = client.post("/query", json={"q": "What is a Job?"}).json()

    assert set(body) == {"question", "answer", "thought_process", "status", "sources", "grounded", "query_id"}
    assert isinstance(body["query_id"], str)


def test_feedback_is_created_then_updated(client, analytics_db):
    query_id = ask(client)

    first = client.post("/feedback", json={"query_id": query_id, "rating": 1, "comment": "Clear answer"})
    second = client.post("/feedback", json={"query_id": query_id, "rating": -1})

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["rating"] == 1 and first.json()["comment"] == "Clear answer"
    assert second.json()["rating"] == -1 and second.json()["comment"] is None
    assert second.json()["created_at"] == first.json()["created_at"]
    stored = rows(analytics_db, "SELECT query_id, rating FROM feedback")
    assert stored == [{"query_id": query_id, "rating": -1}]  # one row per query (UNIQUE)


def test_feedback_for_unknown_query_is_404(client):
    response = client.post("/feedback", json={"query_id": "does-not-exist", "rating": 1})

    assert response.status_code == 404


@pytest.mark.parametrize("rating", [0, 2, -2, "up", None, 1.5])
def test_feedback_rejects_invalid_rating(client, rating):
    response = client.post("/feedback", json={"query_id": ask(client), "rating": rating})

    assert response.status_code == 422


def test_feedback_comment_length_limit(client):
    query_id = ask(client)

    assert client.post("/feedback", json={"query_id": query_id, "rating": 1, "comment": "x" * 1001}).status_code == 422
    assert client.post("/feedback", json={"query_id": query_id, "rating": 1, "comment": "x" * 1000}).status_code == 201


def test_feedback_validation_in_the_store_too(analytics_db):
    with pytest.raises(ValueError):
        analytics.record_feedback("q", 3)
    with pytest.raises(ValueError):
        analytics.record_feedback("q", 1, "x" * 1001)


def test_feedback_when_analytics_is_disabled_is_503(api_app, monkeypatch):
    monkeypatch.setenv("ANALYTICS_ENABLED", "false")

    response = TestClient(api_app.main.app).post("/feedback", json={"query_id": "x", "rating": 1})

    assert response.status_code == 503


def test_feedback_is_rate_limited(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_QUERY", "2/minute")
    query_id = ask(client)  # the query budget is separate

    codes = [client.post("/feedback", json={"query_id": query_id, "rating": 1}).status_code for _ in range(3)]

    assert codes == [201, 201, 429]


# ── GET /analytics ────────────────────────────────────────────────────────────


def test_analytics_is_404_without_a_configured_key(client, monkeypatch):
    monkeypatch.delenv("ANALYTICS_API_KEY", raising=False)

    assert client.get("/analytics", headers={"X-API-Key": "anything"}).status_code == 404


def test_analytics_key_check(client, monkeypatch):
    monkeypatch.setenv("ANALYTICS_API_KEY", "s3cret-key")
    ask(client)

    assert client.get("/analytics").status_code == 401
    assert client.get("/analytics", headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.get("/analytics?days=30", headers={"X-API-Key": "s3cret-key"})
    assert ok.status_code == 200
    assert ok.json()["days"] == 30 and ok.json()["total_queries"] == 1
    assert ok.json()["intents"] == [{"intent": "technical", "queries": 1}]


@pytest.mark.parametrize("days", [0, 91, -1, "week"])
def test_analytics_days_out_of_range_is_422(client, monkeypatch, days):
    monkeypatch.setenv("ANALYTICS_API_KEY", "k")

    assert client.get(f"/analytics?days={days}", headers={"X-API-Key": "k"}).status_code == 422


def test_analytics_hides_questions_when_they_are_not_stored(client, monkeypatch):
    monkeypatch.setenv("ANALYTICS_API_KEY", "k")
    ask(client)
    ask(client)

    assert client.get("/analytics", headers={"X-API-Key": "k"}).json()["top_questions"][0]["times_asked"] == 2
    monkeypatch.setenv("ANALYTICS_STORE_QUESTIONS", "false")
    body = client.get("/analytics", headers={"X-API-Key": "k"}).json()
    assert body["questions_stored"] is False and body["top_questions"] == []


# ── The analytics queries on a seeded dataset ────────────────────────────────

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
# (id, created_at, source, question, intent, attempts, grounded, latency_ms)
SEED = [
    ("q1", "2026-11-01 09:00:00.000000", "api", "What is a Job?", "technical", 1, True, 1000),
    ("q2", "2026-11-01 10:00:00.000000", "api", "what is a job? ", "technical", 2, False, 3000),
    ("q3", "2026-11-03 08:00:00.000000", "stream", "What is a Job?", "technical", 1, None, 2000),
    ("q4", "2026-11-03 09:00:00.000000", "stream", "hello", "conversational", 0, None, 500),
    ("q5", "2026-11-05 11:00:00.000000", "mcp", "tell me a joke", "blocked", 0, None, 100),
    ("q6", "2026-11-07 11:59:00.000000", "mcp", "hello", "conversational", 0, None, 300),
    ("q7", "2026-11-07 06:00:00.000000", "api", "Explain HPA", "error", 0, None, 50),
    ("old", "2026-10-31 23:59:59.999999", "api", "What is a Job?", "technical", 2, True, 9000),  # outside window
]
FEEDBACK = [("q1", 1), ("q2", -1), ("q4", 1), ("old", 1)]


@pytest.fixture
def seeded(analytics_db):
    engine = analytics.get_engine()
    with engine.begin() as conn:
        for qid, created, source, question, intent, attempts, grounded, latency in SEED:
            record = store.QueryRecord(
                qid, created, "t", source, question, intent, attempts, grounded, "ok", latency, 0
            )  # fmt: skip
            store.insert_query_log(conn, record)
        for qid, rating in FEEDBACK:
            conn.execute(
                text(store.UPSERT_FEEDBACK),
                {"id": f"f-{qid}", "query_id": qid, "rating": rating, "comment": None, "now": db.utc_now()},
            )
    return engine


def test_window_start():
    assert queries.window_start(7, NOW) == "2026-11-01 00:00:00.000000"
    assert queries.window_start(1, NOW) == "2026-11-07 00:00:00.000000"


def test_each_query_on_the_seeded_dataset(seeded):
    since = queries.window_start(7, NOW)
    with seeded.connect() as conn:
        assert queries.queries_per_day(conn, since) == [
            {"day": "2026-11-01", "queries": 2},
            {"day": "2026-11-03", "queries": 2},
            {"day": "2026-11-05", "queries": 1},
            {"day": "2026-11-07", "queries": 2},
        ]
        assert queries.intent_breakdown(conn, since) == [
            {"intent": "technical", "queries": 3},
            {"intent": "conversational", "queries": 2},
            {"intent": "blocked", "queries": 1},
            {"intent": "error", "queries": 1},
        ]
        assert queries.grounded_rate(conn, since) == {
            "technical_answers": 3,
            "verified_answers": 2,
            "grounded_answers": 1,
            "grounded_rate": 0.5,
        }
        assert queries.retrieval_stats(conn, since) == {
            "technical_questions": 3,
            "avg_retrieval_attempts": 1.3333,
            "rewritten_questions": 1,
            "rewrite_rate": 0.3333,
        }
        assert queries.feedback_summary(conn, since) == {
            "answers": 7,
            "answers_with_feedback": 3,
            "thumbs_up": 2,
            "thumbs_down": 1,
            "satisfaction_rate": 0.6667,
            "feedback_rate": 0.4286,
        }
        assert queries.top_questions(conn, since) == [
            {"question": "what is a job?", "times_asked": 3},
            {"question": "hello", "times_asked": 2},
        ]
        latency_keys = ("source", "queries", "avg_latency_ms", "max_latency_ms", "p95_latency_ms")
        assert [tuple(r[k] for k in latency_keys) for r in queries.latency_by_source(conn, since)] == [
            ("api", 3, 1350.0, 3000, 3000.0),
            ("mcp", 2, 200.0, 300, 300.0),
            ("stream", 2, 1250.0, 2000, 2000.0),
        ]
        assert set(queries.latency_by_source(conn, since)[0]) == set(latency_keys)


def test_queries_on_an_empty_window(seeded):
    since = "2030-01-01 00:00:00.000000"
    with seeded.connect() as conn:
        assert queries.queries_per_day(conn, since) == []
        assert queries.grounded_rate(conn, since) == {
            "technical_answers": 0,
            "verified_answers": 0,
            "grounded_answers": 0,
            "grounded_rate": None,
        }
        assert queries.feedback_summary(conn, since)["feedback_rate"] is None  # NULLIF: no division by zero
        assert queries.latency_by_source(conn, since) == []


def test_summary_combines_every_query(seeded):
    summary = queries.analytics_summary(seeded, 7, include_questions=True, now=NOW)

    assert summary["total_queries"] == 7
    assert summary["since"] == "2026-11-01 00:00:00.000000"
    assert summary["feedback"]["answers_with_feedback"] == 3
    assert set(summary) == {
        "days", "since", "total_queries", "queries_per_day", "intents", "grounded",
        "retrieval", "feedback", "questions_stored", "top_questions", "latency",
    }  # fmt: skip


def test_percentile_nearest_rank():
    assert queries.percentile([], 0.95) is None
    assert queries.percentile([42], 0.95) == 42.0
    assert queries.percentile(list(range(1, 21)), 0.95) == 19.0
    assert queries.percentile(list(range(1, 101)), 0.95) == 95.0


# ── MCP ───────────────────────────────────────────────────────────────────────


def test_mcp_ask_returns_query_id_and_logs_source_mcp(analytics_db, guardrails, use_agent):
    from tests.test_mcp_server import _call

    use_agent(FakeAgent(output=TECHNICAL_OUTPUT))

    result = _call("ask_knowledge_base", {"question": "What is a Job?"})

    (row,) = rows(analytics_db)
    assert result.structured_content["query_id"] == row["id"] and row["source"] == "mcp"
