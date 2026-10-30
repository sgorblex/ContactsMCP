"""The HTTP app as served: OAuth in front of /mcp, /healthz without auth, Host checks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from contacts_mcp.registry import AccountRegistry
from contacts_mcp.server import build_server, http_app_options, uvicorn_options
from tests._oidc import BASE_URL, fake_discovery, settings
from tests.conftest import FakeStore

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def make_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_registry: Callable[..., AccountRegistry],
) -> Callable[..., httpx.AsyncClient]:
    """``make_app("work")`` → an httpx client on the real HTTP app (no network)."""
    fake_discovery(monkeypatch, tmp_path)

    def factory(*aliases: str, base_url: str = BASE_URL) -> httpx.AsyncClient:
        cfg = settings()
        mcp = build_server(cfg, make_registry(*aliases))
        app = mcp.http_app(**http_app_options(cfg))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)

    return factory


@pytest.fixture
async def client(make_app: Callable[..., httpx.AsyncClient]) -> AsyncIterator[httpx.AsyncClient]:
    async with make_app("work") as c:
        yield c


async def test_mcp_without_token_is_401_with_resource_metadata(client: httpx.AsyncClient) -> None:
    response = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer")
    assert "resource_metadata" in challenge
    assert f"{BASE_URL}/.well-known/oauth-protected-resource/mcp" in challenge


async def test_mcp_with_a_foreign_token_is_401(client: httpx.AsyncClient) -> None:
    headers = {**MCP_HEADERS, "Authorization": "Bearer not-a-token-we-issued"}
    response = await client.post("/mcp", json=INITIALIZE, headers=headers)
    assert response.status_code == 401


async def test_healthz_needs_no_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok: 1 account"


async def test_healthz_reports_unreadable_accounts(
    make_app: Callable[..., httpx.AsyncClient], store: FakeStore
) -> None:
    store.break_("broken")
    async with make_app("work") as c:
        response = await c.get("/healthz")
    assert response.status_code == 503
    assert response.text == "1 unreadable account file(s)"


async def test_healthz_answers_on_loopback_for_the_container_healthcheck(
    make_app: Callable[..., httpx.AsyncClient],
) -> None:
    async with make_app(base_url="http://127.0.0.1:8000") as c:
        response = await c.get("/healthz")
    assert response.status_code == 200
    assert "no accounts linked" in response.text


async def test_protected_resource_metadata(client: httpx.AsyncClient) -> None:
    response = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == f"{BASE_URL}/mcp"
    assert body["authorization_servers"] == [f"{BASE_URL}/"]


async def test_authorization_server_metadata(client: httpx.AsyncClient) -> None:
    response = await client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    body = response.json()
    assert body["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert body["token_endpoint"] == f"{BASE_URL}/token"
    assert body["registration_endpoint"] == f"{BASE_URL}/register"
    assert "S256" in body["code_challenge_methods_supported"]


@pytest.mark.parametrize("path", ["/healthz", "/mcp", "/.well-known/oauth-authorization-server"])
async def test_foreign_host_is_refused(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path, headers={"Host": "evil.example.net"})
    assert response.status_code == 421


async def test_foreign_origin_is_refused(client: httpx.AsyncClient) -> None:
    headers = {**MCP_HEADERS, "Origin": "https://evil.example.net"}
    response = await client.post("/mcp", json=INITIALIZE, headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin", [BASE_URL, "https://claude.ai", "http://localhost:6274", "http://127.0.0.1:5555"]
)
async def test_known_origins_reach_the_auth_check(client: httpx.AsyncClient, origin: str) -> None:
    headers = {**MCP_HEADERS, "Origin": origin}
    response = await client.post("/mcp", json=INITIALIZE, headers=headers)
    assert response.status_code == 401


def test_http_options() -> None:
    cfg = settings()
    options = http_app_options(cfg)
    assert options["path"] == "/mcp"
    assert options["stateless_http"] is True
    assert options["json_response"] is True
    assert options["host_origin_protection"] is True
    assert options["allowed_hosts"] == ["contacts.example.com"]
    assert set(options["allowed_origins"]) == {
        BASE_URL,
        "https://claude.ai",
        "https://claude.com",
        "http://localhost:*",
        "http://127.0.0.1:*",
    }


def test_forwarded_headers_trusted_only_when_configured() -> None:
    cfg = settings()
    assert cfg.http is not None
    assert uvicorn_options(cfg)["proxy_headers"] is False
    # The access log would record /auth/callback?code=…
    assert uvicorn_options(cfg)["access_log"] is False
    assert "forwarded_allow_ips" not in uvicorn_options(cfg)

    trusting = cfg.model_copy(
        update={
            "http": cfg.http.model_copy(
                update={"trust_forwarded": True, "forwarded_allow_ips": "10.0.0.2"}
            )
        }
    )
    options = uvicorn_options(trusting)
    assert options["proxy_headers"] is True
    assert options["forwarded_allow_ips"] == "10.0.0.2"
    assert options["log_config"] is None
