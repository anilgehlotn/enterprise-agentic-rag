import importlib
import sys
from types import SimpleNamespace

import pytest

from app.agents.routing import (
    CONVERSATIONAL,
    is_conversational,
    normalize_planner_decision,
    route_planner,
)


@pytest.mark.parametrize(
    "raw",
    [
        "CONVERSATIONAL",
        "CONVERSATIONAL.",
        '"CONVERSATIONAL"',
        "'CONVERSATIONAL'",
        "`CONVERSATIONAL`",
        "**CONVERSATIONAL**",
        "conversational",
        "  CONVERSATIONAL!\n",
        '"CONVERSATIONAL."',
        "“CONVERSATIONAL”",
    ],
)
def test_decorated_conversational_output_is_normalised(raw):
    assert normalize_planner_decision(raw) == CONVERSATIONAL
    assert route_planner({"current_query": raw}) == "responder"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("kubernetes cronjob schedule syntax", "kubernetes cronjob schedule syntax"),
        ('"kubernetes cronjob schedule syntax"', "kubernetes cronjob schedule syntax"),
        ("  Intel SR-IOV setup?  ", "Intel SR-IOV setup?"),
        # Contains the word but is a real search query, not the sentinel.
        ("conversational AI on Kubernetes", "conversational AI on Kubernetes"),
        ("CONVERSATIONAL agents", "CONVERSATIONAL agents"),
    ],
)
def test_search_queries_are_kept_and_routed_to_retrieval(raw, expected):
    assert normalize_planner_decision(raw) == expected
    assert not is_conversational(raw)
    assert route_planner({"current_query": raw}) == "retriever"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_output_is_not_conversational(raw):
    assert normalize_planner_decision(raw) == ""
    assert route_planner({"current_query": raw}) == "retriever"


def _load_planner_with_fake_llm(monkeypatch, llm_output):
    """Import planner.py with app.gateway stubbed so no LLM client or network is created."""
    fake_llm = SimpleNamespace(invoke=lambda prompt: SimpleNamespace(content=llm_output))
    fake_gateway = SimpleNamespace(get_langchain_llm=lambda **kwargs: fake_llm)
    monkeypatch.setitem(sys.modules, "app.gateway", fake_gateway)
    monkeypatch.delitem(sys.modules, "app.agents.nodes.planner", raising=False)
    return importlib.import_module("app.agents.nodes.planner")


def _state():
    return {"messages": [{"role": "user", "content": "hi there"}]}


@pytest.mark.parametrize("llm_output", ["CONVERSATIONAL.", '"CONVERSATIONAL"', " conversational \n"])
def test_planner_node_normalises_conversational(monkeypatch, llm_output):
    planner = _load_planner_with_fake_llm(monkeypatch, llm_output)

    update = planner.planner_node(_state())

    assert update["current_query"] == CONVERSATIONAL
    assert update["plan"][0] == "Intent: Conversational/Memory"
    assert route_planner(update) == "responder"


def test_planner_node_returns_clean_search_query(monkeypatch):
    planner = _load_planner_with_fake_llm(monkeypatch, '"kubernetes job backoffLimit"\n')

    update = planner.planner_node(_state())

    assert update["current_query"] == "kubernetes job backoffLimit"
    assert update["plan"] == ["Intent: Technical", "Search Term: kubernetes job backoffLimit"]
    assert route_planner(update) == "retriever"
