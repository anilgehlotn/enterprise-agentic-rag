"""FastAPI lifespan: startup opens the checkpointer and warms guardrails; shutdown closes it."""

import importlib
import sqlite3
import sys
import warnings

import pytest
from fastapi.testclient import TestClient

from app.agents import checkpointer as cp
from app.agents import graph as graph_module
from app.services import rag_service


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINTER", "sqlite")
    monkeypatch.setenv("SQLITE_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite"))
    return tmp_path / "checkpoints.sqlite"


def test_startup_opens_and_shutdown_closes_the_checkpointer(api_app, sqlite_env):
    assert cp._handle is None

    with TestClient(api_app.main.app):
        handle = cp._handle
        assert handle is not None and handle.backend == "sqlite"
        assert sqlite_env.is_file()
        handle.saver.conn.execute("SELECT 1")  # open
        assert api_app.ensure_calls == 1  # guardrails warmed

    assert cp._handle is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        handle.saver.conn.execute("SELECT 1")


def test_shutdown_drops_the_cached_agent(api_app):
    with TestClient(api_app.main.app):
        agent = graph_module.get_rag_agent()
        assert agent.checkpointer is cp.get_checkpointer()

    # The cached graph held the closed checkpointer; the next one gets a fresh checkpointer.
    assert graph_module.get_rag_agent() is not agent


def test_invalid_checkpointer_fails_startup(api_app, monkeypatch):
    monkeypatch.setenv("CHECKPOINTER", "redis")

    with pytest.raises(ValueError, match="Invalid CHECKPOINTER"), TestClient(api_app.main.app):
        pass
    assert cp._handle is None


def test_checkpointer_is_closed_when_guardrail_warmup_fails(api_app, sqlite_env, monkeypatch):
    def broken():
        raise RuntimeError("guardrails config error")

    monkeypatch.setattr(rag_service, "ensure_rails", broken)

    with pytest.raises(RuntimeError, match="guardrails config error"), TestClient(api_app.main.app):
        pass
    assert cp._handle is None


def test_no_on_event_deprecation_warnings(api_app):
    """The app uses a lifespan handler; importing and starting it emits no on_event DeprecationWarning."""
    sys.modules.pop("app.main", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        main = importlib.import_module("app.main")
        with TestClient(main.app):
            pass

    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not [m for m in messages if "on_event" in m], messages
    assert main.app.router.on_startup == [] and main.app.router.on_shutdown == []
