"""The settings page: OIDC sign-in in the browser, sessions, forgery checks, account changes."""

from __future__ import annotations

import base64
import html
import json
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
from contacts_mcp.fields import PEOPLE_API_URL, SCOPES
from contacts_mcp.google.oauth import AUTH_URL, REVOKE_URL, TOKEN_URL
from contacts_mcp.linking import LINK_SCOPES
from contacts_mcp.registry import AccountRegistry
from contacts_mcp.server import build_server, http_app_options
from contacts_mcp.web import settings as settings_module
from tests._oidc import BASE_URL, DISCOVERY, fake_discovery, http_settings, id_token

TOKEN_ENDPOINT = DISCOVERY["token_endpoint"]
AUTHORIZE_ENDPOINT = DISCOVERY["authorization_endpoint"]
REFRESH = "1//refresh-token-of-work"
NEW_REFRESH = "1//refresh-token-from-the-page"
GOOGLE_CODE = "4/google-code"
SECRET_VALUES = (REFRESH, NEW_REFRESH, GOOGLE_CODE, "oidc-client-secret", "idp-code", "web-secret")
WEB_CLIENT = GoogleClientConfig(client_id="web-client-id", client_secret=SecretStr("web-secret"))
CONNECTIONS = f"{PEOPLE_API_URL}/people/me/connections"


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
        router.post(TOKEN_URL, name="google_token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "ya29.page",
                    "refresh_token": NEW_REFRESH,
                    "scope": " ".join(LINK_SCOPES),
                    "id_token": google_id_token("new@example.com"),
                },
            )
        )
        router.get(CONNECTIONS, name="people").mock(
            return_value=httpx.Response(200, json={"totalPeople": 0})
        )
        signs_in_as(router, "owner@example.com")
        yield router


def google_id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "iss": "https://accounts.google.com",
                "aud": WEB_CLIENT.client_id,
                "email": email,
                "email_verified": True,
            }
        ).encode()
    )
    return f"e30.{payload.rstrip(b'=').decode()}.sig"


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
        registry = AccountRegistry(store, google, http, web_google=WEB_CLIENT)
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
    assert "form-action 'self' https://auth.example.com https://accounts.google.com" in csp
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


# -- linking --------------------------------------------------------------------------


async def to_google(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTH_URL
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def test_link_a_new_account(
    client: httpx.AsyncClient,
    registry: AccountRegistry,
    store: AccountFileStore,
    idp: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page, csrf = await signed_in_page(client)
    assert 'action="/settings/link"' in page
    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    params = await to_google(
        await post(client, "/settings/link", {"csrf": csrf, "alias": "Home", "read_only": "on"})
    )
    assert params["client_id"] == "web-client-id"
    assert params["redirect_uri"] == f"{BASE_URL}/settings/google/callback"
    assert params["access_type"] == "offline"
    assert params["code_challenge_method"] == "S256"

    response = await client.get(
        "/settings/google/callback", params={"code": GOOGLE_CODE, "state": params["state"]}
    )
    assert response.status_code == 303
    record = store.load("home")
    assert record.client == "web"
    assert record.email == "new@example.com"
    assert record.read_only is True
    assert record.refresh_token.get_secret_value() == NEW_REFRESH
    assert "home" in registry.aliases()
    assert registry.resolve("home").status == "ok"
    exchange = parse_qs(idp["google_token"].calls.last.request.content.decode())
    assert exchange["client_id"] == ["web-client-id"]
    assert exchange["redirect_uri"] == [f"{BASE_URL}/settings/google/callback"]
    text = html.unescape((await client.get("/settings")).text)
    assert "Linked new@example.com as 'home' (read-only)." in text
    for secret in SECRET_VALUES:
        assert secret not in text
    line = next(r for r in caplog.records if getattr(r, "kind", None) == "audit")
    assert line.tool == "settings.link"  # type: ignore[attr-defined]


async def test_link_refuses_a_taken_or_invalid_alias(
    client: httpx.AsyncClient, store: AccountFileStore
) -> None:
    _, csrf = await signed_in_page(client)
    response = await post(client, "/settings/link", {"csrf": csrf, "alias": "work"})
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert "already linked" in (await client.get("/settings")).text
    response = await post(client, "/settings/link", {"csrf": csrf, "alias": "no way"})
    assert response.headers["location"] == "/settings"
    assert "invalid account alias" in (await client.get("/settings")).text


async def test_google_callback_needs_the_session_that_started_it(
    client: httpx.AsyncClient, store: AccountFileStore
) -> None:
    _, csrf = await signed_in_page(client)
    params = await to_google(await post(client, "/settings/link", {"csrf": csrf, "alias": "home"}))
    # Another session (here: the same browser, signed in again) cannot complete it.
    client.cookies.delete("__Host-contacts_session")
    await signed_in_page(client)
    response = await client.get(
        "/settings/google/callback", params={"code": GOOGLE_CODE, "state": params["state"]}
    )
    assert response.status_code == 303
    assert "not started here" in (await client.get("/settings")).text
    assert not store.path("home").exists()


async def test_google_callback_without_a_session(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/settings/google/callback", params={"code": GOOGLE_CODE, "state": "x"}
    )
    assert response.status_code == 403


async def test_link_cancelled_at_google(client: httpx.AsyncClient, store: AccountFileStore) -> None:
    _, csrf = await signed_in_page(client)
    params = await to_google(await post(client, "/settings/link", {"csrf": csrf, "alias": "home"}))
    await client.get(
        "/settings/google/callback", params={"error": "access_denied", "state": params["state"]}
    )
    assert "cancelled" in (await client.get("/settings")).text
    assert not store.path("home").exists()


async def test_link_failure_names_the_fix(
    client: httpx.AsyncClient, store: AccountFileStore, idp: respx.MockRouter
) -> None:
    idp["google_token"].mock(
        return_value=httpx.Response(400, json={"error": "redirect_uri_mismatch"})
    )
    _, csrf = await signed_in_page(client)
    params = await to_google(await post(client, "/settings/link", {"csrf": csrf, "alias": "home"}))
    await client.get(
        "/settings/google/callback", params={"code": GOOGLE_CODE, "state": params["state"]}
    )
    text = (await client.get("/settings")).text
    assert "redirect_uri_mismatch" in text
    assert f"{BASE_URL}/settings/google/callback" in text
    assert not store.path("home").exists()


@pytest.mark.parametrize(
    ("path", "form"),
    [("/settings/link", {"alias": "home"}), ("/settings/accounts/work/relink", {})],
)
@pytest.mark.parametrize("origin", ["https://evil.example", None])
async def test_forged_link_posts_are_refused(
    client: httpx.AsyncClient, path: str, form: dict[str, str], origin: str | None
) -> None:
    _, csrf = await signed_in_page(client)
    response = await post(client, path, {"csrf": csrf, **form}, origin=origin)
    assert response.status_code == 403
    response = await post(client, path, {"csrf": "wrong", **form})
    assert response.status_code == 403


async def test_link_callback_cannot_be_replayed(
    client: httpx.AsyncClient, store: AccountFileStore
) -> None:
    _, csrf = await signed_in_page(client)
    params = await to_google(await post(client, "/settings/link", {"csrf": csrf, "alias": "home"}))
    query = {"code": GOOGLE_CODE, "state": params["state"]}
    await client.get("/settings/google/callback", params=query)
    first = store.load("home").linked_at
    await client.get("/settings/google/callback", params=query)
    assert "expired or was not started here" in (await client.get("/settings")).text
    assert store.load("home").linked_at == first


async def test_alias_linked_meanwhile_is_not_replaced(
    client: httpx.AsyncClient, store: AccountFileStore
) -> None:
    _, csrf = await signed_in_page(client)
    params = await to_google(await post(client, "/settings/link", {"csrf": csrf, "alias": "home"}))
    seed(store, "home")
    await client.get(
        "/settings/google/callback", params={"code": GOOGLE_CODE, "state": params["state"]}
    )
    assert store.load("home").refresh_token.get_secret_value() == REFRESH
    assert "linked meanwhile" in html.unescape((await client.get("/settings")).text)


async def test_relink_of_a_removed_account_is_refused(
    client: httpx.AsyncClient, store: AccountFileStore
) -> None:
    _, csrf = await signed_in_page(client)
    params = await to_google(await post(client, "/settings/accounts/work/relink", {"csrf": csrf}))
    store.delete("work")
    await client.get(
        "/settings/google/callback", params={"code": GOOGLE_CODE, "state": params["state"]}
    )
    assert not store.path("work").exists()
    assert "was removed or its file cannot be read" in html.unescape(
        (await client.get("/settings")).text
    )


async def test_relink_keeps_read_only_and_needs_a_fresh_sign_in(
    client: httpx.AsyncClient, store: AccountFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, "work", read_only=True)
    _, csrf = await signed_in_page(client)

    later = time.time() + 11 * 60

    class Clock:
        @staticmethod
        def time() -> float:
            return later

    with monkeypatch.context() as m:
        m.setattr(settings_module, "time", Clock)
        stale = await post(client, "/settings/accounts/work/relink", {"csrf": csrf})
        assert parse_qs(urlsplit(stale.headers["location"]).query)["max_age"] == ["60"]

    params = await to_google(await post(client, "/settings/accounts/work/relink", {"csrf": csrf}))
    assert params["login_hint"] == "work@example.com"
    await client.get(
        "/settings/google/callback", params={"code": GOOGLE_CODE, "state": params["state"]}
    )
    record = store.load("work")
    assert record.client == "web"
    assert record.read_only is True
    assert record.refresh_token.get_secret_value() == NEW_REFRESH
    assert "linked to work@example.com before" in (await client.get("/settings")).text


async def test_without_a_web_client_the_page_explains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store: AccountFileStore,
    idp: respx.MockRouter,
) -> None:
    fake_discovery(monkeypatch, tmp_path)
    seed(store)
    async with httpx.AsyncClient() as http:
        google = GoogleClientConfig(client_id="client-id", client_secret=SecretStr("secret"))
        registry = AccountRegistry(store, google, http)
        registry.reload()
        async with app_client(registry, make_settings()) as c:
            page, csrf = await signed_in_page(c)
            assert "GOOGLE_WEB_CLIENT_ID" in page
            assert "Re-link" not in page
            response = await post(c, "/settings/link", {"csrf": csrf, "alias": "home"})
            assert response.headers["location"] == "/settings"
