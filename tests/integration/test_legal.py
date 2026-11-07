"""The /v3/legal routes, with GitHub replaced by a fake."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.v3.endpoints import legal
from app.main import app
from app.utils import cache as cache_module
from app.utils import github_proxy as gp
from app.utils.cache import MemoryCache

REPO = "acme/legal"
RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
TERMS_V1 = "---\nversion: 1.0.0\neffective_date: 01/01/2027\n---\n# Terms one\n"
TERMS_V2 = "---\nversion: 2.0.0\neffective_date: 01/06/2027\n---\n# Terms two\n"
PRIVACY = "---\nversion: 1.0.0\neffective_date: 01/01/2027\n---\n# Privacy\n"


class Reply:
    def __init__(self, status=200, body="", headers=None):
        self.status_code = status
        self.text = body if isinstance(body, str) else ""
        self._body = body
        self.headers = httpx.Headers(headers or {})

    def json(self):
        return json.loads(self._body) if isinstance(self._body, str) else self._body


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


def releases(*tags):
    return Reply(body=[{"tag_name": t} for t in tags])


def raw(tag, name):
    return f"https://raw.githubusercontent.com/{REPO}/{tag}/{name}"


async def no_sleep(_seconds):
    return None


@pytest.fixture(autouse=True)
def github(monkeypatch):
    cache = MemoryCache()
    monkeypatch.setattr(cache_module, "get_cache_backend", lambda: cache)
    monkeypatch.setattr(gp, "get_cache_backend", lambda: cache)
    monkeypatch.setattr(gp.httpx, "AsyncClient", FakeGitHub)
    gp._local_counters.clear()
    FakeGitHub.calls = []
    FakeGitHub.routes = {
        RELEASES: releases(
            "privacy-policy-v1.0.0",
            "terms-of-service-v1.0.0",
            "terms-of-service-v0.9.0",
        ),
        raw("terms-of-service-v1.0.0", "terms_of_service_EN.md"): Reply(body=TERMS_V1),
        raw("privacy-policy-v1.0.0", "privacy_policy_EN.md"): Reply(body=PRIVACY),
    }
    monkeypatch.delenv("SWI_LEGAL_REPO", raising=False)
    return cache


@pytest.fixture
def client():
    return TestClient(app)


def test_placeholder_when_no_repo_is_configured(client):
    body = client.get("/v3/legal/privacy-policy").json()
    assert body["source"] == "placeholder"
    assert body["document"] == "privacy-policy"
    assert FakeGitHub.calls == []


def test_released_text_is_served_from_the_newest_tag(client, monkeypatch):
    monkeypatch.setenv("SWI_LEGAL_REPO", REPO)
    body = client.get("/v3/legal/terms-of-service").json()
    assert body["source"] == "release"
    assert body["tag"] == "terms-of-service-v1.0.0"
    assert body["version"] == "1.0.0"
    assert body["effective_date"] == "01/01/2027"
    assert body["content"] == "# Terms one\n"


def test_many_requests_cause_one_set_of_github_calls(client, monkeypatch):
    monkeypatch.setenv("SWI_LEGAL_REPO", REPO)
    for _ in range(25):
        assert client.get("/v3/legal/terms-of-service").status_code == 200
        assert client.get("/v3/legal/privacy-policy").status_code == 200
    assert len(FakeGitHub.calls) == 3  # releases + one file per document


def test_a_new_release_replaces_the_text_on_refresh_only(client, monkeypatch):
    monkeypatch.setenv("SWI_LEGAL_REPO", REPO)
    client.get("/v3/legal/terms-of-service")
    FakeGitHub.routes[RELEASES] = releases(
        "terms-of-service-v2.0.0", "privacy-policy-v1.0.0"
    )
    FakeGitHub.routes[raw("terms-of-service-v2.0.0", "terms_of_service_EN.md")] = Reply(
        body=TERMS_V2
    )
    assert client.get("/v3/legal/terms-of-service").json()["version"] == "1.0.0"

    asyncio.run(legal.refresh_legal())
    assert client.get("/v3/legal/terms-of-service").json()["version"] == "2.0.0"


def test_unchanged_tag_is_not_downloaded_again(client, monkeypatch):
    monkeypatch.setenv("SWI_LEGAL_REPO", REPO)
    client.get("/v3/legal/terms-of-service")
    FakeGitHub.calls.clear()

    asyncio.run(legal.refresh_legal())
    assert FakeGitHub.calls == [RELEASES]


def test_last_good_copy_is_served_while_github_is_down(client, monkeypatch):
    monkeypatch.setenv("SWI_LEGAL_REPO", REPO)
    client.get("/v3/legal/terms-of-service")
    FakeGitHub.routes = {}

    with pytest.raises(gp.UpstreamUnavailable):
        asyncio.run(legal.refresh_legal())
    response = client.get("/v3/legal/terms-of-service")
    assert response.status_code == 200
    assert response.json()["version"] == "1.0.0"


def test_503_without_a_copy_when_github_is_down(client, monkeypatch):
    monkeypatch.setenv("SWI_LEGAL_REPO", REPO)
    monkeypatch.setattr(gp.asyncio, "sleep", no_sleep)
    FakeGitHub.routes = {}
    response = client.get("/v3/legal/terms-of-service")
    assert response.status_code == 503
    assert "github" not in response.text.lower()
    assert REPO not in response.text


def test_a_document_without_a_release_is_unavailable_not_a_placeholder(
    client, monkeypatch
):
    monkeypatch.setenv("SWI_LEGAL_REPO", REPO)
    monkeypatch.setattr(gp.asyncio, "sleep", no_sleep)
    FakeGitHub.routes[RELEASES] = releases("privacy-policy-v1.0.0")
    assert client.get("/v3/legal/privacy-policy").status_code == 200
    assert client.get("/v3/legal/terms-of-service").status_code == 503


def test_invalid_repo_setting_serves_placeholders(client, monkeypatch):
    monkeypatch.setenv("SWI_LEGAL_REPO", "https://evil.test/x")
    assert client.get("/v3/legal/privacy-policy").json()["source"] == "placeholder"
    assert FakeGitHub.calls == []


def test_etag_gives_a_304(client):
    first = client.get("/v3/legal/privacy-policy")
    again = client.get(
        "/v3/legal/privacy-policy", headers={"If-None-Match": first.headers["etag"]}
    )
    assert again.status_code == 304
    assert "max-age=3600" in first.headers["cache-control"]


@pytest.mark.parametrize(
    "path", ["/v3/legal/other", "/v3/legal/privacy-policy?lang=xx", "/v3/legal/..%2Fx"]
)
def test_unknown_document_or_language_is_rejected(client, path):
    assert client.get(path).status_code in (404, 422)
