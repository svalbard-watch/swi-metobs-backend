"""
Fetch files from GitHub on behalf of visitors, without ever hitting GitHub's rate limits.

Visitors never reach GitHub (their IP address stays with us) and never cause a GitHub
request either: endpoints only read the shared cache, which is filled by a refresh that
runs at start-up and then every SWI_GITHUB_REFRESH_HOURS. Every fetch goes through
`fetch`, which enforces:

- a fixed list of hosts (no caller-supplied URLs);
- a per-hour call budget per host, well under GitHub's limits;
- a backoff after any failure or 403/429, honouring Retry-After / X-RateLimit-Reset;
- conditional requests (ETag), so an unchanged file transfers nothing.

The last good copy is kept for 30 days and served while GitHub is unreachable. A single
worker refreshes per cycle (lock in the shared cache).
"""

import asyncio
import json
import os
import time
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

import httpx
from loguru import logger

from app.utils.cache import get_cache_backend, shared_cache_get, shared_cache_set

API_HOST = "api.github.com"
RAW_HOST = "raw.githubusercontent.com"
ALLOWED_HOSTS = {API_HOST, RAW_HOST}

# Cached copies outlive any outage: only a successful refresh replaces them.
KEEP_TTL = 30 * 24 * 3600
# One worker refreshes per cycle. The lock is left to expire (not released), so workers
# that start a few seconds apart do not each run a cycle.
LOCK_TTL = 120
MIN_BACKOFF = 300
LOOP_TICK = 300
TIMEOUT = 10.0
USER_AGENT = "swi-metobs-backend"

BACKOFF_KEY = "github:backoff"
LAST_CYCLE_KEY = "github:last-cycle"

_local_counters: dict[str, int] = {}

Refresher = Callable[[], Awaitable[None]]


class UpstreamUnavailable(Exception):
    """GitHub could not (or must not) be reached. The message is safe to log, not to show."""


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def refresh_interval() -> int:
    """Seconds between two scheduled refreshes."""
    try:
        hours = float(os.getenv("SWI_GITHUB_REFRESH_HOURS", "6"))
    except ValueError:
        hours = 6.0
    return max(int(hours * 3600), MIN_BACKOFF)


def _budget(host: str) -> int:
    if host == API_HOST:
        return _int_env("SWI_GITHUB_MAX_CALLS_PER_HOUR", 20)
    return _int_env("SWI_GITHUB_MAX_RAW_CALLS_PER_HOUR", 120)


def _increment(key: str, ttl: int) -> int:
    """Atomically count in Redis, or in process when Redis is not used."""
    backend = get_cache_backend()
    try:
        if backend is not None and hasattr(backend, "incr"):
            count = int(backend.incr(key))
            if count == 1:
                backend.expire(key, ttl)
            return count
        if backend is not None:
            count = int(backend.get(key) or 0) + 1
            backend.setex(key, ttl, str(count))
            return count
    except Exception as e:
        logger.warning(f"GitHub budget counter unavailable ({e}), counting in process")
    _local_counters[key] = _local_counters.get(key, 0) + 1
    return _local_counters[key]


def _spend(host: str) -> bool:
    """Count one call to `host` in the current hour; False when the budget is used up."""
    window = int(time.time() // 3600)
    return _increment(f"github:budget:{host}:{window}", 3700) <= _budget(host)


def try_lock(name: str, ttl: int) -> bool:
    """Take a lock shared by all workers. The lock is never released, it expires."""
    backend = get_cache_backend()
    if backend is None:
        return True
    key = f"github:lock:{name}"
    try:
        if hasattr(backend, "incr"):
            return bool(backend.set(key, "1", nx=True, ex=ttl))
        if backend.get(key):
            return False
        backend.setex(key, ttl, "1")
        return True
    except Exception as e:
        logger.warning(f"GitHub lock unavailable ({e}), continuing without it")
        return True


def _backoff_until() -> float:
    state = shared_cache_get(BACKOFF_KEY) or {}
    return float(state.get("until", 0))


def _set_backoff(response: Optional[httpx.Response]) -> None:
    state = shared_cache_get(BACKOFF_KEY) or {}
    fails = int(state.get("fails", 0))
    delay = min(MIN_BACKOFF * 2**fails, refresh_interval())
    if response is not None:
        try:
            if "retry-after" in response.headers:
                delay = max(delay, int(response.headers["retry-after"]))
            elif response.headers.get("x-ratelimit-remaining") == "0":
                reset = int(response.headers["x-ratelimit-reset"])
                delay = max(delay, int(reset - time.time()))
        except (ValueError, KeyError):
            pass
    shared_cache_set(
        BACKOFF_KEY, {"until": time.time() + delay, "fails": fails + 1}, delay + 60
    )
    logger.warning(f"GitHub unavailable, no new request for {delay}s")


def _clear_backoff() -> None:
    if _backoff_until() or (shared_cache_get(BACKOFF_KEY) or {}).get("fails"):
        shared_cache_set(BACKOFF_KEY, {"until": 0, "fails": 0}, 60)


def get_entry(name: str) -> Optional[dict]:
    """The last good copy: {"value", "etag", "fetched_at"}, or None."""
    return shared_cache_get(f"github:entry:{name}")


def store(name: str, value: Any, etag: Optional[str] = None) -> dict:
    entry = {"value": value, "etag": etag, "fetched_at": time.time()}
    shared_cache_set(f"github:entry:{name}", entry, KEEP_TTL)
    return entry


async def fetch(name: str, url: str, *, as_json: bool = False) -> dict:
    """Refresh the cached copy `name` from `url` and return it.

    Raises UpstreamUnavailable without calling GitHub when backing off or over budget.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"Not an allowed GitHub URL: {url}")
    host = parts.hostname

    if _backoff_until() > time.time():
        raise UpstreamUnavailable("backing off after an earlier failure")
    if not _spend(host):
        raise UpstreamUnavailable(f"hourly call budget for {host} used up")

    entry = get_entry(name)
    headers = {"User-Agent": USER_AGENT}
    if host == API_HOST:
        headers["Accept"] = "application/vnd.github+json"
        token = os.getenv("SWI_GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    if entry and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        _set_backoff(None)
        raise UpstreamUnavailable(
            f"request to {host} failed: {type(e).__name__}"
        ) from e

    if response.status_code == 304 and entry:
        _clear_backoff()
        return store(name, entry["value"], entry.get("etag"))
    if response.status_code != 200:
        _set_backoff(response)
        raise UpstreamUnavailable(f"{host} answered {response.status_code}")

    try:
        value = response.json() if as_json else response.text
    except json.JSONDecodeError as e:
        _set_backoff(None)
        raise UpstreamUnavailable(f"{host} returned invalid JSON") from e
    _clear_backoff()
    return store(name, value, response.headers.get("etag"))


async def run_cycle(refreshers: list[Refresher], *, force: bool = False) -> bool:
    """Run every refresher once if this worker wins the cycle. Returns whether it ran.

    `force` ignores the age of the last cycle (start-up, cold cache) but not the lock,
    the backoff or the budget.
    """
    if not force and shared_cache_get(LAST_CYCLE_KEY):
        return False
    if not try_lock("cycle", LOCK_TTL):
        return False
    ok = True
    for refresh in refreshers:
        try:
            await refresh()
        except UpstreamUnavailable as e:
            ok = False
            logger.warning(f"GitHub refresh skipped: {e}")
        except Exception:
            ok = False
            logger.exception("GitHub refresh failed")
    shared_cache_set(
        LAST_CYCLE_KEY, {"at": time.time()}, refresh_interval() if ok else MIN_BACKOFF
    )
    return True


async def refresh_loop(refreshers: list[Refresher]) -> None:
    """Refresh at start-up (so a redeploy never serves what the old version cached), then
    whenever the last cycle is older than the refresh interval."""
    await run_cycle(refreshers, force=True)
    while True:
        await asyncio.sleep(LOOP_TICK)
        await run_cycle(refreshers)


async def get_or_populate(
    name: str, refreshers: list[Refresher], wait: float = 10.0
) -> Optional[dict]:
    """The cached copy `name`. On a cold cache, run one shared cycle and wait for it."""
    entry = get_entry(name)
    if entry is not None:
        return entry
    await run_cycle(refreshers, force=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        entry = get_entry(name)
        if entry is not None:
            return entry
        await asyncio.sleep(0.5)
    return get_entry(name)
