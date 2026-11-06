"""Per-client-IP rate limits on /query, /query/stream and /documents (no network)."""

import pytest
from fastapi.testclient import TestClient

from app import rate_limit
from app.rate_limit import SlidingWindowLimiter, parse_rate
from app.services import rag_service

OK_ANSWER = {"question": "q", "answer": "a", "thought_process": [], "status": "ok", "sources": [], "grounded": None}


@pytest.fixture
def api(api_app, monkeypatch):
    """The API with the agent mocked out and a small query limit of 3/minute."""
    monkeypatch.setenv("RATE_LIMIT_QUERY", "3/minute")
    monkeypatch.setenv("RATE_LIMIT_UPLOAD", "2/minute")
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setattr(rag_service, "answer_question", lambda q, t: OK_ANSWER)
    monkeypatch.setattr(rag_service, "stream_answer", lambda q, t: iter([("done", {"status": "ok"})]))
    return TestClient(api_app.main.app)


def ask(client, path="/query", **headers):
    return client.post(path, json={"q": "What is a Job?"}, headers=headers)


# ── parse_rate ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [("20/minute", (20, 60)), ("5/minutes", (5, 60)), (" 1/Second ", (1, 1)), ("100/hour", (100, 3600))],
)
def test_parse_rate(value, expected):
    assert parse_rate(value) == expected


@pytest.mark.parametrize("value", ["20", "x/minute", "20/fortnight", "0/minute", "-1/minute", ""])
def test_parse_rate_rejects_bad_values(value):
    with pytest.raises(ValueError, match="Invalid rate limit"):
        parse_rate(value)


def test_defaults(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_QUERY", raising=False)
    monkeypatch.delenv("RATE_LIMIT_UPLOAD", raising=False)

    assert rate_limit.configured_rate(rate_limit.QUERY_LIMIT_ENV, rate_limit.QUERY_LIMIT_DEFAULT) == (20, 60)
    assert rate_limit.configured_rate(rate_limit.UPLOAD_LIMIT_ENV, rate_limit.UPLOAD_LIMIT_DEFAULT) == (5, 60)


# ── Sliding window ────────────────────────────────────────────────────────────


def test_sliding_window_frees_slots_as_old_hits_expire():
    now = [1000.0]
    limiter = SlidingWindowLimiter(clock=lambda: now[0])

    assert limiter.hit("k", 2, 60) is None
    now[0] += 30
    assert limiter.hit("k", 2, 60) is None
    assert limiter.hit("k", 2, 60) == pytest.approx(30)  # first hit expires in 30 s
    now[0] += 30.5
    assert limiter.hit("k", 2, 60) is None  # first hit left the window
    assert limiter.hit("other", 2, 60) is None  # keys are independent


def test_sweep_keeps_clients_a_longer_window_still_needs(monkeypatch):
    monkeypatch.setattr(rate_limit, "_SWEEP_THRESHOLD", 1)
    now = [0.0]
    limiter = SlidingWindowLimiter(clock=lambda: now[0])
    limiter.hit("upload:a", 1, 3600)
    now[0] += 120
    limiter.hit("query:b", 5, 60)
    limiter.hit("query:c", 5, 60)  # triggers a sweep

    assert limiter.hit("upload:a", 1, 3600) is not None  # still limited after the sweep


# ── Endpoints ─────────────────────────────────────────────────────────────────


def test_query_n_plus_one_is_429(api):
    for _ in range(3):
        assert ask(api).status_code == 200

    response = ask(api)

    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json()["detail"]
    assert int(response.headers["Retry-After"]) >= 1


def test_query_and_stream_share_one_budget(api):
    assert ask(api).status_code == 200
    assert ask(api, "/query/stream").status_code == 200
    assert ask(api).status_code == 200

    assert ask(api, "/query/stream").status_code == 429


def test_uploads_have_their_own_limit(api):
    def upload():
        return api.post("/documents", files={"files": ("a.exe", b"x", "application/octet-stream")})

    # 415 = the request got past the limiter and reached validation.
    assert [upload().status_code for _ in range(3)] == [415, 415, 429]
    assert ask(api).status_code == 200  # the query budget is untouched


def test_health_is_never_limited(api):
    for _ in range(3):
        ask(api)
    assert ask(api).status_code == 429

    assert all(api.get("/health").status_code == 200 for _ in range(50))


def test_invalid_payloads_count_too(api):
    """Route dependencies run before body validation, so a flood of junk requests is throttled as well."""
    codes = [api.post("/query", json={"q": ""}).status_code for _ in range(4)]

    assert codes == [422, 422, 422, 429]


# ── X-Forwarded-For ───────────────────────────────────────────────────────────


def test_x_forwarded_for_is_ignored_by_default(api):
    """A client rotating a fake X-Forwarded-For must not get a fresh budget."""
    codes = [ask(api, **{"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(4)]

    assert codes == [200, 200, 200, 429]


def test_x_forwarded_for_is_used_when_trusted(api, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")

    for _ in range(3):
        assert ask(api, **{"X-Forwarded-For": "203.0.113.7"}).status_code == 200
    assert ask(api, **{"X-Forwarded-For": "203.0.113.7"}).status_code == 429
    # A different real client (as written by the proxy) has its own budget.
    assert ask(api, **{"X-Forwarded-For": "198.51.100.2"}).status_code == 200


def test_trusted_header_uses_the_proxy_written_rightmost_entry(api, monkeypatch):
    """The left entries are client-supplied even behind a proxy; spoofing them changes nothing."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")

    codes = [ask(api, **{"X-Forwarded-For": f"6.6.6.{i}, 203.0.113.7"}).status_code for i in range(4)]

    assert codes == [200, 200, 200, 429]


def test_trusted_without_header_falls_back_to_peer_address(api, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")

    assert [ask(api).status_code for _ in range(4)] == [200, 200, 200, 429]


# ── Startup validation ────────────────────────────────────────────────────────


def test_startup_rejects_an_invalid_limit(api_app, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_QUERY", "lots")

    with pytest.raises(ValueError, match="Invalid rate limit"), TestClient(api_app.main.app):
        pass
