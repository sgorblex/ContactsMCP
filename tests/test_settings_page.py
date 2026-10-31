"""The settings page: OIDC sign-in in the browser, sessions, forgery checks, account changes."""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from pydantic import SecretStr

from contacts_mcp.accounts import AccountFileStore, AccountRecord
from contacts_mcp.config import GoogleClientConfig, Settings
from contacts_mcp.fields import SCOPES
from contacts_mcp.google.oauth import REVOKE_URL
from contacts_mcp.registry import AccountRegistry
from contacts_mcp.server import build_server, http_app_options
from contacts_mcp.web import settings as settings_module
from tests._oidc import BASE_URL, DISCOVERY, fake_discovery, http_settings, id_token

TOKEN_ENDPOINT = DISCOVERY["token_endpoint"]
AUTHORIZE_ENDPOINT = DISCOVERY["authorization_endpoint"]
REFRESH = "1//refresh-token-of-work"
SECRET_VALUES = (REFRESH, "oidc-client-secret", "idp-code")


def make_settings(*, settings_page: bool = True) -> Settings:
    return Settings(
        transport="http",
        server_name="contacts",
        data_dir=Path("/nonexistent"),
        exposed_tools="all",
        log_level="INFO",
        http=http_settings(settings_page=settings_page),
    )


def seed(store: AccountFileStore, alias: str = "work", *, read_only: bool = False) -> None:
    store.save(
        AccountRecord(
            alias=alias,
            email=f"{alias}@example.com",
            refresh_token=SecretStr(REFRESH),
            scopes=list(SCOPES),
            read_only=read_only,
            linked_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        )
    )


@pytest.fixture
def idp() -> Iterator[respx.MockRouter]:
    """The identity provider's token endpoint and Google's revoke endpoint."""
    with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
        router.post(TOKEN_ENDPOINT, name="token")
        router.post(REVOKE_URL, name="revoke").mock(return_value=httpx.Response(200))
        signs_in_as(router, "owner@example.com")
        yield router


def signs_in_as(
    router: respx.MockRouter,
    email: str,
    *,
    auth_time: float | None = None,
    with_auth_time: bool = True,
) -> None:
    claims: dict[str, Any] = {"sub": "s1", "email": email, "email_verified": True}
    if with_auth_time:
        claims["auth_time"] = int(auth_time if auth_time is not None else time.time())
    body = {"access_token": "at", "id_token": id_token(**claims)}
    router["token"].mock(return_value=httpx.Response(200, json=body))


@pytest.fixture
def store(tmp_path: Path) -> AccountFileStore:
    return AccountFileStore(tmp_path / "data")


@pytest.fixture
async def registry(store: AccountFileStore) -> AsyncIterator[AccountRegistry]:
    async with httpx.AsyncClient() as http:
        google = GoogleClientConfig(client_id="client-id", client_secret=SecretStr("secret"))
        registry = AccountRegistry(store, google, http)
        registry.reload()
        yield registry


def app_client(registry: AccountRegistry, settings: Settings) -> httpx.AsyncClient:
    mcp = build_server(settings, registry)
    app = mcp.http_app(**http_app_options(settings))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registry: AccountRegistry,
    store: AccountFileStore,
    idp: respx.MockRouter,
) -> AsyncIterator[httpx.AsyncClient]:
    fake_discovery(monkeypatch, tmp_path)
    seed(store)
    registry.reload()
    async with app_client(registry, make_settings()) as c:
        yield c


async def start(client: httpx.AsyncClient, path: str = "/settings") -> dict[str, str]:
    """GET a page without a session: the redirect to the provider, as query parameters."""
    response = await client.get(path)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTHORIZE_ENDPOINT
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def sign_in(client: httpx.AsyncClient, path: str = "/settings") -> httpx.Response:
    params = await start(client, path)
    return await client.get(
        "/settings/auth/callback", params={"code": "idp-code", "state": params["state"]}
    )


async def signed_in_page(client: httpx.AsyncClient) -> tuple[str, str]:
    """Sign in and return the page and its CSRF token."""
    response = await sign_in(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    page = await client.get("/settings")
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match is not None
    return page.text, match.group(1)


def post(
    client: httpx.AsyncClient, path: str, form: dict[str, str], *, origin: str | None = BASE_URL
) -> Any:
    headers = {"Origin": origin} if origin else {}
    return client.post(path, data=form, headers=headers)


# -- sign-in --------------------------------------------------------------------------


async def test_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: AccountRegistry
) -> None:
    fake_discovery(monkeypatch, tmp_path)
    async with app_client(registry, make_settings(settings_page=False)) as c:
        assert (await c.get("/settings")).status_code == 404


async def test_redirects_to_the_provider_with_pkce(client: httpx.AsyncClient) -> None:
    params = await start(client)
    assert params["client_id"] == "oidc-client-id"
    assert params["redirect_uri"] == f"{BASE_URL}/settings/auth/callback"
    assert params["response_type"] == "code"
    assert params["scope"] == "openid email"
    assert params["code_challenge_method"] == "S256"
    assert "max_age" not in params
    assert client.cookies.get("__Host-contacts_signin") == params["state"]


async def test_sign_in_and_page(client: httpx.AsyncClient, idp: respx.MockRouter) -> None:
    page, _ = await signed_in_page(client)
    assert "owner@example.com" in page
    assert "work@example.com" in page
    assert "2026-01-02 03:04 UTC" in page
    for secret in SECRET_VALUES:
        assert secret not in page
    # PKCE and client authentication reached the token endpoint.
    request = idp["token"].calls.last.request
    form = parse_qs(request.content.decode())
    assert form["code_verifier"][0]
    assert form["redirect_uri"] == [f"{BASE_URL}/settings/auth/callback"]
    assert request.headers["authorization"].startswith("Basic ")


async def test_page_headers(client: httpx.AsyncClient) -> None:
    await signed_in_page(client)
    response = await client.get("/settings")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "form-action 'self' https://auth.example.com" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


async def test_callback_from_another_browser_is_refused(client: httpx.AsyncClient) -> None:
    params = await start(client)
    client.cookies.clear()
    response = await client.get(
        "/settings/auth/callback", params={"code": "idp-code", "state": params["state"]}
    )
    assert response.status_code == 403
    assert (await client.get("/settings")).status_code == 303


async def test_state_cannot_be_replayed(client: httpx.AsyncClient) -> None:
    params = await start(client)
    first = await client.get(
        "/settings/auth/callback", params={"code": "idp-code", "state": params["state"]}
    )
    assert first.status_code == 303
    client.cookies.set("__Host-contacts_signin", params["state"], domain="contacts.example.com")
    again = await client.get(
        "/settings/auth/callback", params={"code": "idp-code", "state": params["state"]}
    )
    assert again.status_code == 403


async def test_user_not_in_the_allowlist_is_refused(
    client: httpx.AsyncClient, idp: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    signs_in_as(idp, "intruder@example.com")
    response = await sign_in(client)
    assert response.status_code == 403
    assert "not allowed" in response.text
    assert (await client.get("/settings")).status_code == 303
    assert any("intruder@example.com" in str(getattr(r, "user", "")) for r in caplog.records)


async def test_provider_error_is_shown(client: httpx.AsyncClient) -> None:
    params = await start(client)
    response = await client.get(
        "/settings/auth/callback", params={"error": "access_denied", "state": params["state"]}
    )
    assert response.status_code == 403
    assert "access_denied" in response.text


async def test_token_endpoint_failure_is_shown(
    client: httpx.AsyncClient, idp: respx.MockRouter
) -> None:
    idp["token"].mock(return_value=httpx.Response(500))
    response = await sign_in(client)
    assert response.status_code == 403
    assert "refused the sign-in" in response.text
    assert "__Host-contacts_session" not in response.headers.get("set-cookie", "")


async def test_sign_out(client: httpx.AsyncClient) -> None:
    _, csrf = await signed_in_page(client)
    response = await post(client, "/settings/sign-out", {"csrf": csrf})
    assert response.status_code == 200
    assert (await client.get("/settings")).status_code == 303


async def test_idle_session_expires(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await signed_in_page(client)
    later = time.time() + 31 * 60
    monkeypatch.setattr("contacts_mcp.web.session.time.time", lambda: later)
    assert (await client.get("/settings")).status_code == 303


# -- changes ------------------------------------------------------------------------


async def test_set_read_only(
    client: httpx.AsyncClient,
    registry: AccountRegistry,
    store: AccountFileStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, csrf = await signed_in_page(client)
    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    response = await post(
        client, "/settings/accounts/work/read-only", {"csrf": csrf, "value": "on"}
    )
    assert response.status_code == 303
    assert store.load("work").read_only is True
    assert registry.resolve("work").read_only is True
    assert "now read-only" in (await client.get("/settings")).text
    line = next(r for r in caplog.records if getattr(r, "kind", None) == "audit")
    assert line.tool == "settings.set_read_only"  # type: ignore[attr-defined]
    assert line.user == "owner@example.com"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("form", "origin", "fetch_site"),
    [
        ({}, BASE_URL, None),
        ({"csrf": "wrong"}, BASE_URL, None),
        (None, None, None),
        (None, None, "cross-site"),
        (None, "https://evil.example", None),
    ],
)
async def test_forged_posts_are_refused(
    client: httpx.AsyncClient,
    store: AccountFileStore,
    form: dict[str, str] | None,
    origin: str | None,
    fetch_site: str | None,
) -> None:
    _, csrf = await signed_in_page(client)
    headers = {k: v for k, v in {"Origin": origin, "Sec-Fetch-Site": fetch_site}.items() if v}
    response = await client.post(
        "/settings/accounts/work/read-only",
        data={"value": "on", **(form if form is not None else {"csrf": csrf})},
        headers=headers,
    )
    assert response.status_code == 403
    assert store.load("work").read_only is False


async def test_same_origin_fetch_without_origin_header(
    client: httpx.AsyncClient, store: AccountFileStore
) -> None:
    _, csrf = await signed_in_page(client)
    response = await client.post(
        "/settings/accounts/work/read-only",
        data={"csrf": csrf, "value": "on"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 303
    assert store.load("work").read_only is True


async def test_remove_after_a_recent_sign_in(
    client: httpx.AsyncClient,
    registry: AccountRegistry,
    store: AccountFileStore,
    idp: respx.MockRouter,
) -> None:
    _, csrf = await signed_in_page(client)
    confirm = await client.get("/settings/accounts/work/remove")
    assert "work@example.com" in confirm.text
    response = await post(client, "/settings/accounts/work/remove", {"csrf": csrf})
    assert response.status_code == 303
    assert not store.path("work").exists()
    assert registry.aliases() == []
    assert parse_qs(idp["revoke"].calls.last.request.content.decode())["token"] == [REFRESH]
    assert "Removed 'work'" in html.unescape((await client.get("/settings")).text)


async def test_remove_is_audited(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    _, csrf = await signed_in_page(client)
    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    await post(client, "/settings/accounts/work/remove", {"csrf": csrf})
    line = next(r for r in caplog.records if getattr(r, "kind", None) == "audit")
    assert line.tool == "settings.remove"  # type: ignore[attr-defined]
    assert line.outcome == "ok"  # type: ignore[attr-defined]
    assert line.detail == "revoke: revoked"  # type: ignore[attr-defined]


async def test_oversize_form_is_refused(client: httpx.AsyncClient, store: AccountFileStore) -> None:
    _, csrf = await signed_in_page(client)
    form = {"csrf": csrf, "value": "on", "pad": "x" * 5000}
    response = await post(client, "/settings/accounts/work/read-only", form)
    assert response.status_code == 403
    assert store.load("work").read_only is False


async def test_sign_in_without_auth_time_is_never_fresh(
    client: httpx.AsyncClient, idp: respx.MockRouter, store: AccountFileStore
) -> None:
    signs_in_as(idp, "owner@example.com", with_auth_time=False)
    _, csrf = await signed_in_page(client)
    response = await post(client, "/settings/accounts/work/remove", {"csrf": csrf})
    assert response.status_code == 303
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    callback = await client.get(
        "/settings/auth/callback", params={"code": "idp-code", "state": state}
    )
    assert callback.status_code == 403
    assert "no auth_time" in callback.text
    assert store.path("work").exists()


async def test_remove_needs_a_fresh_sign_in(
    client: httpx.AsyncClient, store: AccountFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, csrf = await signed_in_page(client)
    later = time.time() + 11 * 60

    class Clock:
        @staticmethod
        def time() -> float:
            return later

    monkeypatch.setattr(settings_module, "time", Clock)
    response = await post(client, "/settings/accounts/work/remove", {"csrf": csrf})
    assert response.status_code == 303
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["max_age"] == ["60"]
    assert store.path("work").exists()


async def test_step_up_needs_a_new_login_at_the_provider(
    client: httpx.AsyncClient,
    idp: respx.MockRouter,
    store: AccountFileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, csrf = await signed_in_page(client)
    later = time.time() + 11 * 60

    class Clock:
        @staticmethod
        def time() -> float:
            return later

    monkeypatch.setattr(settings_module, "time", Clock)
    response = await post(client, "/settings/accounts/work/remove", {"csrf": csrf})
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    # The provider answered from its own session: auth_time is long before the request.
    signs_in_as(idp, "owner@example.com", auth_time=time.time() - 3600)
    callback = await client.get(
        "/settings/auth/callback", params={"code": "idp-code", "state": state}
    )
    assert callback.status_code == 403
    assert "did not ask you to sign in again" in callback.text
    assert store.path("work").exists()


async def test_unknown_or_invalid_alias(client: httpx.AsyncClient) -> None:
    _, csrf = await signed_in_page(client)
    response = await post(client, "/settings/accounts/Not_Valid/read-only", {"csrf": csrf})
    assert response.status_code == 404
    response = await post(client, "/settings/accounts/nobody/read-only", {"csrf": csrf})
    assert response.status_code == 303
    assert "Could not change 'nobody'" in html.unescape((await client.get("/settings")).text)
