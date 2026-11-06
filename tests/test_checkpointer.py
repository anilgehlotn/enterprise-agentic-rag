"""Checkpointer factory, SQLite persistence across a rebuilt agent, and thread reset.

No network: memory and sqlite are real (sqlite on a temp file); postgres is mocked
at the module level, so neither psycopg nor a database is needed.
"""

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agents import checkpointer as cp
from app.agents.graph import build_graph
from app.agents.routing import CONVERSATIONAL
from app.agents.state import new_request_state
from app.services import rag_service

# ── Fake-node graph (conversational path: planner → responder) ────────────────


def _planner(state):
    return {"current_query": CONVERSATIONAL, "plan": ["Intent: Conversational/Memory"]}


def _responder(state):
    question = state["messages"][-1]["content"]
    answer = f"echo: {question}"
    return {"final_answer": answer, "messages": [{"role": "assistant", "content": answer}]}


def _graph(saver):
    return build_graph(planner=_planner, retriever=lambda s: {}, responder=_responder, checkpointer=saver)


def _thread(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def _user_messages(graph, thread_id):
    messages = graph.get_state(_thread(thread_id)).values.get("messages", [])
    return [m["content"] for m in messages if m["role"] == "user"]


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "dir" / "checkpoints.sqlite"
    monkeypatch.setenv("CHECKPOINTER", "sqlite")
    monkeypatch.setenv("SQLITE_CHECKPOINT_PATH", str(path))
    return path


# ── resolve_checkpointer_backend / build_checkpointer ─────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [(None, "sqlite"), ("", "sqlite"), ("memory", "memory"), (" SQLite ", "sqlite"), ("postgres", "postgres")],
)
def test_backend_parsing(monkeypatch, value, expected):
    monkeypatch.delenv("CHECKPOINTER", raising=False)
    if value is not None:
        monkeypatch.setenv("CHECKPOINTER", value)

    assert cp.resolve_checkpointer_backend() == expected


@pytest.mark.parametrize("value", ["redis", "mem", "postgresql"])
def test_invalid_backend_is_a_clear_error(monkeypatch, value):
    monkeypatch.setenv("CHECKPOINTER", value)

    with pytest.raises(ValueError, match=r"Invalid CHECKPOINTER=.*memory, sqlite, postgres"):
        cp.build_checkpointer()


def test_memory_backend():
    handle = cp.build_checkpointer("memory")

    assert handle.backend == "memory"
    assert isinstance(handle.saver, MemorySaver)
    handle.close()  # no-op, must not raise


def test_sqlite_backend_creates_folder_and_tables(sqlite_env):
    handle = cp.build_checkpointer()
    try:
        assert handle.backend == "sqlite"
        assert isinstance(handle.saver, SqliteSaver)
        assert sqlite_env.is_file()  # parent folders created
        tables = {row[0] for row in handle.saver.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "checkpoints" in tables  # setup() ran at build time
    finally:
        handle.close()

    with pytest.raises(Exception, match="closed"):
        handle.saver.conn.execute("SELECT 1")


def test_sqlite_default_path_is_under_the_project_root_not_the_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("SQLITE_CHECKPOINT_PATH", raising=False)
    monkeypatch.chdir(tmp_path)  # e.g. Claude Desktop starting the MCP server elsewhere

    path = cp.sqlite_checkpoint_path()

    assert path == cp.PROJECT_ROOT / "state" / "checkpoints.sqlite"
    assert (cp.PROJECT_ROOT / "app" / "agents" / "checkpointer.py").is_file()


def test_sqlite_explicit_path_is_used_as_given(monkeypatch):
    monkeypatch.setenv("SQLITE_CHECKPOINT_PATH", "relative/memory.sqlite")

    assert cp.sqlite_checkpoint_path().as_posix() == "relative/memory.sqlite"


# ── postgres (mocked) ─────────────────────────────────────────────────────────


@pytest.fixture
def fake_postgres(monkeypatch):
    """Stub psycopg, psycopg_pool and langgraph.checkpoint.postgres; record what the factory does."""
    log = SimpleNamespace(pool_args=None, setup_calls=0, pool_closed=0, setup_error=None)

    class FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, conninfo, **kwargs):
            log.pool_args = (conninfo, kwargs)

        def close(self):
            log.pool_closed += 1

    class FakePostgresSaver:
        def __init__(self, conn):
            self.conn = conn

        def setup(self):
            log.setup_calls += 1
            if log.setup_error:
                raise log.setup_error

    dict_row = object()
    log.dict_row = dict_row
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "psycopg.rows", SimpleNamespace(dict_row=dict_row))
    monkeypatch.setitem(sys.modules, "psycopg_pool", SimpleNamespace(ConnectionPool=FakePool))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", SimpleNamespace(PostgresSaver=FakePostgresSaver))
    monkeypatch.setenv("CHECKPOINTER", "postgres")
    return log


def test_postgres_requires_database_url(fake_postgres, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        cp.build_checkpointer()


def test_postgres_builds_pool_and_runs_setup_once(fake_postgres, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example/rag")

    handle = cp.build_checkpointer()

    conninfo, kwargs = fake_postgres.pool_args
    assert conninfo == "postgresql://u:p@db.example/rag"
    # The connection settings PostgresSaver requires.
    assert kwargs["kwargs"] == {"autocommit": True, "prepare_threshold": 0, "row_factory": fake_postgres.dict_row}
    assert kwargs["check"] is not None  # idle connections dropped by the server are replaced
    assert fake_postgres.setup_calls == 1
    assert handle.backend == "postgres"

    handle.close()
    assert fake_postgres.pool_closed == 1


def test_postgres_setup_failure_closes_the_pool(fake_postgres, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example/rag")
    fake_postgres.setup_error = RuntimeError("permission denied for schema public")

    with pytest.raises(RuntimeError, match="permission denied"):
        cp.build_checkpointer()
    assert fake_postgres.pool_closed == 1


# ── process-wide singleton ────────────────────────────────────────────────────


def test_open_is_idempotent_and_close_reopens(sqlite_env):
    first = cp.open_checkpointer()
    assert cp.open_checkpointer() is first
    assert cp.get_checkpointer() is first.saver

    cp.close_checkpointer()
    cp.close_checkpointer()  # second close is a no-op

    second = cp.open_checkpointer()
    assert second is not first


# ── memory persists across a rebuilt agent (sqlite) ───────────────────────────


def test_sqlite_memory_survives_rebuilding_the_agent(sqlite_env):
    first = cp.build_checkpointer()
    _graph(first.saver).invoke(new_request_state("my name is Ada"), config=_thread("X"))
    first.close()  # simulates an API restart

    second = cp.build_checkpointer()  # same file, new connection, new graph
    try:
        graph = _graph(second.saver)
        assert _user_messages(graph, "X") == ["my name is Ada"]

        final = graph.invoke(new_request_state("what is my name?"), config=_thread("X"))

        assert [m["content"] for m in final["messages"]] == [
            "my name is Ada",
            "echo: my name is Ada",
            "what is my name?",
            "echo: what is my name?",
        ]
        assert _user_messages(graph, "Y") == []  # threads are isolated
    finally:
        second.close()


def test_memory_backend_forgets_on_rebuild():
    """The contrast: MemorySaver lives in the process, so a new instance starts empty."""
    _graph(cp.build_checkpointer("memory").saver).invoke(new_request_state("hi"), config=_thread("X"))

    assert _user_messages(_graph(cp.build_checkpointer("memory").saver), "X") == []


# ── reset ─────────────────────────────────────────────────────────────────────


def test_reset_thread_clears_only_that_thread(sqlite_env):
    graph = _graph(cp.get_checkpointer())
    graph.invoke(new_request_state("keep me"), config=_thread("other"))
    graph.invoke(new_request_state("forget me"), config=_thread("X"))

    rag_service.reset_thread("X")

    assert _user_messages(graph, "X") == []
    assert _user_messages(graph, "other") == ["keep me"]
    # A fresh conversation starts cleanly on the reset thread.
    final = graph.invoke(new_request_state("new start"), config=_thread("X"))
    assert [m["content"] for m in final["messages"]] == ["new start", "echo: new start"]


def test_reset_endpoint_clears_the_conversation(api_app, sqlite_env):
    main = api_app.main
    with TestClient(main.app) as client:  # lifespan opens the sqlite checkpointer
        graph = _graph(cp.get_checkpointer())
        graph.invoke(new_request_state("remember this"), config=_thread("abc-123"))
        assert _user_messages(graph, "abc-123") == ["remember this"]

        response = client.post("/threads/abc-123/reset")

        assert response.status_code == 200
        assert response.json() == {"thread_id": "abc-123", "status": "cleared"}
        assert _user_messages(graph, "abc-123") == []


def test_reset_endpoint_unknown_thread_is_fine(api_app):
    assert TestClient(api_app.main.app).post("/threads/never-used/reset").status_code == 200


def test_reset_endpoint_backend_failure_is_503(api_app, monkeypatch):
    def broken(thread_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rag_service, "reset_thread", broken)

    response = TestClient(api_app.main.app).post("/threads/abc/reset")

    assert response.status_code == 503
    assert "database is locked" not in response.text  # no internals leak to the client


def test_reset_endpoint_rejects_overlong_thread_id(api_app):
    assert TestClient(api_app.main.app).post(f"/threads/{'x' * 201}/reset").status_code == 422
