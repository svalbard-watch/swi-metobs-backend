"""The /v3/project routes, with GitHub replaced by a fake."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.v3.endpoints import project
from app.main import app
from app.utils import cache as cache_module
from app.utils import github_proxy as gp
from app.utils.cache import MemoryCache

CONTRIBUTORS = [{"name": "Ada", "contributions": 3}]
REFERENCES = [{"id": "r1", "title": "A reference"}]
RELEASE = {"release": "2.0.0"}


class Reply:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.headers = httpx.Headers(headers or {})

    def json(self):
        return self._body


class FakeGitHub:
    routes: dict = {}
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        FakeGitHub.calls.append(url)
        return FakeGitHub.routes.get(url, Reply(404))


@pytest.fixture(autouse=True)
def github(monkeypatch):
    cache = MemoryCache()
    monkeypatch.setattr(cache_module, "get_cache_backend", lambda: cache)
    monkeypatch.setattr(gp, "get_cache_backend", lambda: cache)
    monkeypatch.setattr(gp.httpx, "AsyncClient", FakeGitHub)
    monkeypatch.setattr(gp, "COLD_WAIT", 0)
    gp._local_counters.clear()
    FakeGitHub.calls = []
    FakeGitHub.routes = {
        project.DEFAULT_URLS["contributors"]: Reply(body=CONTRIBUTORS),
        project.DEFAULT_URLS["references"]: Reply(body=REFERENCES),
        project.DEFAULT_URLS["release"]: Reply(body=RELEASE),
    }
    for name in project.DEFAULT_URLS:
        monkeypatch.delenv(f"SWI_PROJECT_INFO_{name.upper()}_URL", raising=False)
    return cache


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("contributors", CONTRIBUTORS),
        ("references", REFERENCES),
        ("release", RELEASE),
    ],
)
def test_json_is_passed_through(client, name, expected):
    response = client.get(f"/v3/project/{name}")
    assert response.status_code == 200
    assert response.json() == expected
    assert "max-age=3600" in response.headers["cache-control"]


def test_many_requests_cause_one_call_per_file(client):
    for _ in range(20):
        for name in project.DEFAULT_URLS:
            assert client.get(f"/v3/project/{name}").status_code == 200
    assert len(FakeGitHub.calls) == 3


def test_source_can_be_overridden_but_stays_on_github(client, monkeypatch):
    other = "https://raw.githubusercontent.com/acme/info/main/people.json"
    FakeGitHub.routes[other] = Reply(body=[{"name": "Grace"}])
    monkeypatch.setenv("SWI_PROJECT_INFO_CONTRIBUTORS_URL", other)
    assert client.get("/v3/project/contributors").json() == [{"name": "Grace"}]


def test_source_outside_github_is_never_fetched(client, monkeypatch):
    monkeypatch.setenv("SWI_PROJECT_INFO_RELEASE_URL", "https://evil.test/v.json")
    assert client.get("/v3/project/release").status_code == 503
    assert "https://evil.test/v.json" not in FakeGitHub.calls


def test_last_good_copy_survives_an_outage(client):
    client.get("/v3/project/contributors")
    FakeGitHub.routes = {}
    with pytest.raises(gp.UpstreamUnavailable):
        asyncio.run(project.refresh_project())
    assert client.get("/v3/project/contributors").json() == CONTRIBUTORS


def test_503_without_a_copy_does_not_leak_the_source(client):
    FakeGitHub.routes = {}
    response = client.get("/v3/project/contributors")
    assert response.status_code == 503
    assert "github" not in response.text.lower()


def test_etag_gives_a_304(client):
    first = client.get("/v3/project/release")
    again = client.get(
        "/v3/project/release", headers={"If-None-Match": first.headers["etag"]}
    )
    assert again.status_code == 304


def test_unknown_name_is_rejected(client):
    assert client.get("/v3/project/anything").status_code == 422
