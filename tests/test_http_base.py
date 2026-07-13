"""HTTP hardening: retry on transient errors, 404 caching, cache short-circuit,
and the per-provider circuit breaker. No real network — the session is faked."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tag_creator import clients
from tag_creator.clients import base as base_mod
from tag_creator.clients.base import CircuitBreaker, ProviderClient
from tag_creator.csv_store import CsvStore
from tag_creator.rate_limit import RateLimiter


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class FakeSession:
    """Replays a scripted list of responses (or exceptions) for get()."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.headers = {}

    def _next(self):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, *a, **k):
        return self._next()

    def post(self, *a, **k):
        return self._next()

    def mount(self, *a, **k):
        pass


def _client(monkeypatch, responses, provider="testprov"):
    store = CsvStore(Path(tempfile.mkdtemp()))
    client = ProviderClient(store, RateLimiter({}))
    client.provider_name = provider
    client.session = FakeSession(responses)
    # never actually sleep during retry/backoff
    monkeypatch.setattr(base_mod.time, "sleep", lambda *_: None)
    return client, store


@pytest.fixture(autouse=True)
def _fresh_breaker(monkeypatch):
    # Isolate the module-global breaker per test.
    monkeypatch.setattr(base_mod, "BREAKER", CircuitBreaker(threshold=5))


def test_retries_then_succeeds_on_500(monkeypatch):
    client, store = _client(monkeypatch, [
        FakeResponse(500, text="err"),
        FakeResponse(200, json_data={"ok": True}),
    ])
    data = client.get_json("https://x/api")
    assert data == {"ok": True}
    assert client.session.calls == 2
    store.close()


def test_429_is_retried(monkeypatch):
    client, store = _client(monkeypatch, [
        FakeResponse(429, headers={"Retry-After": "0"}),
        FakeResponse(200, json_data={"v": 1}),
    ])
    assert client.get_json("https://x/api") == {"v": 1}
    assert client.session.calls == 2
    store.close()


def test_404_returns_empty_and_is_cached(monkeypatch):
    client, store = _client(monkeypatch, [FakeResponse(404)])
    assert client.get_json("https://x/missing") == {}
    # Second call must hit the cache (no further session calls left, so a network
    # attempt would raise IndexError).
    assert client.get_json("https://x/missing") == {}
    assert client.session.calls == 1
    store.close()


def test_successful_response_is_cached(monkeypatch):
    client, store = _client(monkeypatch, [FakeResponse(200, json_data={"a": 1})])
    assert client.get_json("https://x/api") == {"a": 1}
    assert client.get_json("https://x/api") == {"a": 1}  # served from cache
    assert client.session.calls == 1
    store.close()


def test_circuit_breaker_opens_after_threshold(monkeypatch):
    # 3 connection errors per call, threshold 2 -> breaker trips, later calls skip.
    monkeypatch.setattr(base_mod, "BREAKER", CircuitBreaker(threshold=2))
    import requests
    errors = [requests.RequestException("down")] * 9
    client, store = _client(monkeypatch, errors)
    client.get_json("https://x/1")  # 3 attempts -> 1 failure recorded
    client.get_json("https://x/2")  # 3 attempts -> 2nd failure -> breaker OPEN
    calls_before = client.session.calls
    client.get_json("https://x/3")  # breaker open -> short-circuits, no session call
    assert client.session.calls == calls_before
    assert base_mod.BREAKER.is_open("testprov")
    store.close()


def test_non_json_response_is_a_failure(monkeypatch):
    client, store = _client(monkeypatch, [FakeResponse(200, json_data=ValueError("not json"))])
    assert client.get_json("https://x/api") is None
    store.close()
