"""Tests for the rate-limit-safe GitHub fetcher (no network)."""

from datetime import datetime

import httpx
import pytest

from app.utils import cache as cache_module
from app.utils import github_proxy as gp
from app.utils.cache import MemoryCache

RAW = "https://raw.githubusercontent.com/o/r/main/file.txt"
API = "https://api.github.com/repos/o/r/releases"


class FakeResponse:
    def __init__(self, status=200, body="hello", headers=None):
        self.status_code = status
        self._body = body
        self.headers = httpx.Headers(headers or {})

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else ""

    def json(self):
        if isinstance(self._body, str):
            import json

            return json.loads(self._body)
        return self._body


class FakeClient:
    """Stands in for httpx.AsyncClient and records every call."""

    responses: list = []
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        FakeClient.calls.append((url, dict(headers or {})))
        item = FakeClient.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def shared_cache(monkeypatch):
    cache = MemoryCache()
    monkeypatch.setattr(cache_module, "get_cache_backend", lambda: cache)
    monkeypatch.setattr(gp, "get_cache_backend", lambda: cache)
    monkeypatch.setattr(gp.httpx, "AsyncClient", FakeClient)
    gp._local_counters.clear()
    FakeClient.responses = []
    FakeClient.calls = []
    for var in (
        "SWI_GITHUB_TOKEN",
        "SWI_GITHUB_MAX_CALLS_PER_HOUR",
        "SWI_GITHUB_MAX_RAW_CALLS_PER_HOUR",
        "SWI_GITHUB_REFRESH_HOURS",
    ):
        monkeypatch.delenv(var, raising=False)
    return cache


class TestFetch:
    async def test_stores_the_body_and_etag(self):
        FakeClient.responses = [FakeResponse(body="doc", headers={"etag": '"a"'})]
        entry = await gp.fetch("n", RAW)
        assert entry["value"] == "doc"
        assert gp.get_entry("n")["etag"] == '"a"'

    async def test_json_body(self):
        FakeClient.responses = [FakeResponse(body='[{"a": 1}]')]
        entry = await gp.fetch("n", RAW, as_json=True)
        assert entry["value"] == [{"a": 1}]

    async def test_unchanged_file_keeps_the_copy_via_etag(self):
        FakeClient.responses = [
            FakeResponse(body="doc", headers={"etag": '"a"'}),
            FakeResponse(status=304),
        ]
        await gp.fetch("n", RAW)
        entry = await gp.fetch("n", RAW)
        assert entry["value"] == "doc"
        assert FakeClient.calls[1][1]["If-None-Match"] == '"a"'

    @pytest.mark.parametrize(
        "url",
        [
            "http://raw.githubusercontent.com/o/r/main/f",
            "https://example.com/f",
            "https://raw.githubusercontent.com.evil.test/f",
        ],
    )
    async def test_rejects_other_hosts_and_plain_http(self, url):
        with pytest.raises(ValueError):
            await gp.fetch("n", url)
        assert FakeClient.calls == []

    async def test_token_is_only_sent_to_the_api(self, monkeypatch):
        monkeypatch.setenv("SWI_GITHUB_TOKEN", "secret")
        FakeClient.responses = [FakeResponse(body="[]"), FakeResponse(body="x")]
        await gp.fetch("a", API, as_json=True)
        await gp.fetch("b", RAW)
        assert FakeClient.calls[0][1]["Authorization"] == "Bearer secret"
        assert "Authorization" not in FakeClient.calls[1][1]

    async def test_no_visitor_data_is_forwarded(self):
        FakeClient.responses = [FakeResponse()]
        await gp.fetch("n", RAW)
        assert set(FakeClient.calls[0][1]) == {"User-Agent"}


class TestRateLimitSafety:
    async def test_budget_stops_calls_before_the_limit(self, monkeypatch):
        monkeypatch.setenv("SWI_GITHUB_MAX_CALLS_PER_HOUR", "2")
        FakeClient.responses = [FakeResponse(body="[]") for _ in range(5)]
        await gp.fetch("a", API, as_json=True)
        await gp.fetch("a", API, as_json=True)
        with pytest.raises(gp.UpstreamUnavailable, match="budget"):
            await gp.fetch("a", API, as_json=True)
        assert len(FakeClient.calls) == 2

    async def test_raw_and_api_have_separate_budgets(self, monkeypatch):
        monkeypatch.setenv("SWI_GITHUB_MAX_CALLS_PER_HOUR", "1")
        FakeClient.responses = [FakeResponse(body="[]"), FakeResponse()]
        await gp.fetch("a", API, as_json=True)
        await gp.fetch("b", RAW)

    async def test_failure_starts_a_backoff_that_blocks_every_call(self):
        FakeClient.responses = [FakeResponse(status=500)]
        with pytest.raises(gp.UpstreamUnavailable):
            await gp.fetch("n", RAW)
        with pytest.raises(gp.UpstreamUnavailable, match="backing off"):
            await gp.fetch("other", API)
        assert len(FakeClient.calls) == 1

    async def test_retry_after_is_honoured(self):
        FakeClient.responses = [
            FakeResponse(status=429, headers={"retry-after": "7200"})
        ]
        with pytest.raises(gp.UpstreamUnavailable):
            await gp.fetch("n", RAW)
        assert gp._backoff_until() - gp.time.time() > 7000

    async def test_rate_limit_reset_is_honoured(self):
        reset = str(int(gp.time.time()) + 5000)
        FakeClient.responses = [
            FakeResponse(
                status=403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
            )
        ]
        with pytest.raises(gp.UpstreamUnavailable):
            await gp.fetch("n", API)
        assert gp._backoff_until() - gp.time.time() > 4000

    async def test_network_error_backs_off_without_leaking_the_url(self):
        FakeClient.responses = [httpx.ConnectError("boom https://secret")]
        with pytest.raises(gp.UpstreamUnavailable) as err:
            await gp.fetch("n", RAW)
        assert "secret" not in str(err.value)
        assert gp._backoff_until() > gp.time.time()

    async def test_backoff_grows_then_clears_on_success(self, shared_cache):
        gp._set_backoff(None)
        first = gp._backoff_until() - gp.time.time()
        gp._set_backoff(None)
        assert gp._backoff_until() - gp.time.time() > first * 1.5
        shared_cache.delete(gp.BACKOFF_KEY)
        FakeClient.responses = [FakeResponse()]
        await gp.fetch("n", RAW)
        assert gp._backoff_until() == 0


class TestCycle:
    async def test_runs_every_refresher(self):
        seen = []

        async def one():
            seen.append(1)

        async def two():
            seen.append(2)

        assert await gp.run_cycle([one, two], force=True) is True
        assert seen == [1, 2]

    async def test_only_one_worker_runs_a_cycle(self):
        calls = []

        async def refresh():
            calls.append(1)

        assert await gp.run_cycle([refresh], force=True) is True
        assert await gp.run_cycle([refresh], force=True) is False
        assert calls == [1]

    async def test_scheduled_cycle_waits_for_the_interval(self, shared_cache):
        calls = []

        async def refresh():
            calls.append(1)

        await gp.run_cycle([refresh], force=True)
        shared_cache.delete("github:lock:cycle")
        assert await gp.run_cycle([refresh]) is False
        shared_cache.delete(gp.LAST_CYCLE_KEY)
        assert await gp.run_cycle([refresh]) is True
        assert calls == [1, 1]

    async def test_a_failing_refresher_does_not_stop_the_others(self):
        seen = []

        async def broken():
            raise gp.UpstreamUnavailable("down")

        async def fine():
            seen.append(1)

        await gp.run_cycle([broken, fine], force=True)
        assert seen == [1]

    async def test_failed_cycle_is_retried_soon(self, shared_cache):
        async def broken():
            raise gp.UpstreamUnavailable("down")

        await gp.run_cycle([broken], force=True)
        marker = shared_cache._cache[gp.LAST_CYCLE_KEY]["expires_at"]
        assert (marker - datetime.now()).total_seconds() <= gp.MIN_BACKOFF

    async def test_cold_cache_populates_once_for_concurrent_requests(self):
        import asyncio

        async def refresh():
            await asyncio.sleep(0.05)
            gp.store("x", "v")

        results = await asyncio.gather(
            *[gp.get_or_populate("x", [refresh], wait=2) for _ in range(10)]
        )
        assert all(r["value"] == "v" for r in results)

    async def test_warm_cache_never_refreshes(self):
        gp.store("x", "v")

        async def refresh():
            raise AssertionError("must not refresh")

        assert (await gp.get_or_populate("x", [refresh]))["value"] == "v"
