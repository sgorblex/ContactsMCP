"""TokenProvider: caching, shared refresh, failure mapping, secrecy."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from contacts_mcp.config import GoogleClientConfig
from contacts_mcp.errors import ContactsError, GoogleApiError, NeedsRelinkError
from contacts_mcp.google import oauth
from contacts_mcp.google.oauth import TOKEN_URL, TokenProvider
from tests.conftest import ACCESS_TOKEN

REFRESH = "1//refresh-secret-value"


async def instant(_: float) -> None:
    pass


@pytest.fixture
def provider(http: httpx.AsyncClient, google_client: GoogleClientConfig) -> TokenProvider:
    return TokenProvider("work", google_client, REFRESH, http, sleep=instant)


async def test_posts_the_refresh_grant(google: respx.MockRouter, provider: TokenProvider) -> None:
    route = google.routes[0]
    assert await provider.access_token() == ACCESS_TOKEN
    form = parse_qs(route.calls.last.request.content.decode())
    assert form == {
        "grant_type": ["refresh_token"],
        "client_id": ["client-id"],
        "client_secret": ["client-secret"],
        "refresh_token": [REFRESH],
    }


async def test_token_is_cached_across_calls(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    route = google.routes[0]
    for _ in range(10):
        assert await provider.access_token() == ACCESS_TOKEN
    assert route.call_count == 1


async def test_token_is_refreshed_after_expiry(
    google: respx.MockRouter, provider: TokenProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = google.routes[0]
    clock = [1000.0]
    monkeypatch.setattr(
        "contacts_mcp.google.oauth.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    await provider.access_token()
    clock[0] += 3599 - oauth.EXPIRY_MARGIN - 1  # still valid
    await provider.access_token()
    assert route.call_count == 1
    clock[0] += 2  # inside the safety margin
    await provider.access_token()
    assert route.call_count == 2


async def test_force_refresh_fetches_a_new_token(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    route = google.routes[0]
    await provider.access_token()
    await provider.access_token(force_refresh=True)
    assert route.call_count == 2


async def test_concurrent_callers_share_one_refresh(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    gate = asyncio.Event()

    async def slow(_: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200, json={"access_token": "shared", "expires_in": 3600})

    route = google.post(TOKEN_URL).mock(side_effect=slow)
    tasks = [asyncio.create_task(provider.access_token()) for _ in range(8)]
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(*tasks) == ["shared"] * 8
    assert route.call_count == 1


async def test_concurrent_forced_refreshes_after_a_401_share_one_refresh(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    await provider.access_token()

    async def slow(_request: httpx.Request) -> httpx.Response:
        for _ in range(5):
            await asyncio.sleep(0)  # let the other callers queue on the lock
        return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3600})

    route = google.post(TOKEN_URL).mock(side_effect=slow)
    before = route.call_count
    # Google may hand out the same token string again: sharing must not depend on it.
    await asyncio.gather(*(provider.access_token(force_refresh=True) for _ in range(5)))
    assert route.call_count - before == 1


async def test_invalid_grant_needs_relink_and_stops_contacting_google(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    route = google.post(TOKEN_URL).respond(
        400, json={"error": "invalid_grant", "error_description": "Token has been revoked."}
    )
    with pytest.raises(NeedsRelinkError) as excinfo:
        await provider.access_token()
    assert "contacts-mcp auth add work" in str(excinfo.value)
    assert provider.invalid
    assert route.call_count == 1
    for _ in range(3):
        with pytest.raises(NeedsRelinkError):
            await provider.access_token()
        with pytest.raises(NeedsRelinkError):
            await provider.access_token(force_refresh=True)
    assert route.call_count == 1


async def test_invalid_grant_after_a_good_token_discards_it(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    await provider.access_token()
    google.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(NeedsRelinkError):
        await provider.access_token(force_refresh=True)
    with pytest.raises(NeedsRelinkError):
        await provider.access_token()


@pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
async def test_invalid_client_names_the_credentials_not_the_account(
    google: respx.MockRouter, provider: TokenProvider, error: str
) -> None:
    google.post(TOKEN_URL).respond(401, json={"error": error})
    with pytest.raises(GoogleApiError) as excinfo:
        await provider.access_token()
    assert not isinstance(excinfo.value, NeedsRelinkError)
    assert "GOOGLE_CLIENT_ID" in str(excinfo.value)
    assert error in str(excinfo.value)
    assert not provider.invalid  # fixing the client config recovers without re-linking


async def test_server_errors_are_retried_then_fail_briefly(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    route = google.post(TOKEN_URL).respond(503, text="<html>SECRET-BODY</html>")
    with pytest.raises(GoogleApiError, match="HTTP 503") as excinfo:
        await provider.access_token()
    assert route.call_count == oauth.REFRESH_ATTEMPTS
    assert "SECRET-BODY" not in str(excinfo.value)
    assert not provider.invalid


async def test_transient_failure_then_success(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    route = google.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"access_token": "later", "expires_in": 3600}),
        ]
    )
    assert await provider.access_token() == "later"
    assert route.call_count == 3


async def test_unreachable_google_is_a_google_api_error(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    google.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(GoogleApiError, match="Could not reach Google"):
        await provider.access_token()
    assert not provider.invalid


async def test_garbage_response_is_a_google_api_error(
    google: respx.MockRouter, provider: TokenProvider
) -> None:
    google.post(TOKEN_URL).respond(200, text="not json")
    with pytest.raises(GoogleApiError):
        await provider.access_token()


async def test_secrets_never_appear_in_errors_or_logs(
    google: respx.MockRouter,
    provider: TokenProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    messages: list[str] = []
    for response in (
        httpx.Response(400, json={"error": "invalid_grant", "error_description": REFRESH}),
        httpx.Response(401, json={"error": "invalid_client", "error_description": "client-secret"}),
        httpx.Response(503, text=f"{REFRESH} client-secret"),
        httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3600}),
    ):
        google.post(TOKEN_URL).mock(return_value=response)
        fresh = TokenProvider("work", provider._client, REFRESH, provider._http, sleep=instant)
        try:
            messages.append(await fresh.access_token())
        except ContactsError as e:
            messages.append(str(e))
    errors = messages[:3]
    for text in (*errors, caplog.text):
        for secret in (REFRESH, "client-secret", ACCESS_TOKEN):
            assert secret not in text, secret
    assert messages[3] == ACCESS_TOKEN
