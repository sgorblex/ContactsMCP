"""The user allowlist: matching rules, identity extraction, and where it is enforced."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from contacts_mcp.oidc import AllowlistOIDCProxy, build_auth, decode_jwt_payload, is_allowed
from tests._oidc import USERINFO_URL, fake_discovery, http_settings, id_token

ALLOWED = frozenset({"owner@example.com", "sub-allowed"})


# -- is_allowed -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "x", "email": "owner@example.com", "email_verified": True},
        {"sub": "x", "email": "Owner@Example.COM", "email_verified": True},
        {"sub": "x", "email": " owner@example.com ", "email_verified": True},
        {"sub": "sub-allowed"},
        {"sub": "sub-allowed", "email": "someone@example.net", "email_verified": False},
    ],
)
def test_allowed(claims: dict[str, Any]) -> None:
    assert is_allowed(claims, ALLOWED)


@pytest.mark.parametrize(
    "claims",
    [
        None,
        {},
        {"sub": "x", "email": "owner@example.com", "email_verified": False},
        {"sub": "x", "email": "owner@example.com"},
        {"sub": "x", "email": "owner@example.com", "email_verified": "true"},
        {"sub": "x", "email": "intruder@example.com", "email_verified": True},
        {"sub": "SUB-ALLOWED"},
        {"sub": "", "email": "", "email_verified": True},
        {"email": "OWNER@example.com", "email_verified": False},
        # An e-mail entry never matches as a subject (providers that use the e-mail as sub).
        {"sub": "owner@example.com", "email_verified": False},
        {"sub": "owner@example.com", "email": "owner@example.com", "email_verified": False},
        # A subject entry never matches as an e-mail (custom claim mappings).
        {"sub": "x", "email": "sub-allowed", "email_verified": True},
    ],
)
def test_refused(claims: dict[str, Any] | None) -> None:
    assert not is_allowed(claims, ALLOWED)


def test_empty_allowlist_refuses_everyone() -> None:
    assert not is_allowed({"sub": "x", "email": "a@b.c", "email_verified": True}, frozenset())


def test_decode_jwt_payload() -> None:
    assert decode_jwt_payload(id_token(sub="s1"))["sub"] == "s1"  # type: ignore[index]
    assert decode_jwt_payload("not-a-jwt") is None
    assert decode_jwt_payload("a.!!!.c") is None


# -- the provider -----------------------------------------------------------------------


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AllowlistOIDCProxy:
    fake_discovery(monkeypatch, tmp_path)
    return build_auth(http_settings(allowed_users=ALLOWED))


async def test_claims_from_id_token(proxy: AllowlistOIDCProxy) -> None:
    tokens = {
        "access_token": "at",
        "id_token": id_token(sub="s1", email="Owner@Example.com", email_verified=True),
    }
    claims = await proxy._extract_upstream_claims(tokens)
    assert claims == {"sub": "s1", "email": "Owner@Example.com", "email_verified": True}


async def test_string_email_verified_is_normalized(proxy: AllowlistOIDCProxy) -> None:
    tokens = {"id_token": id_token(sub="s1", email="a@b.c", email_verified="true")}
    claims = await proxy._extract_upstream_claims(tokens)
    assert claims is not None
    assert claims["email_verified"] is True


async def test_id_token_for_another_client_is_ignored(proxy: AllowlistOIDCProxy) -> None:
    tokens = {"id_token": id_token(sub="s1", aud="someone-else")}
    with respx.mock(assert_all_mocked=True):
        assert await proxy._extract_upstream_claims(tokens) is None


async def test_userinfo_fallback_without_id_token(proxy: AllowlistOIDCProxy) -> None:
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(USERINFO_URL).mock(
            return_value=httpx.Response(
                200, json={"sub": "s2", "email": "owner@example.com", "email_verified": True}
            )
        )
        claims = await proxy._extract_upstream_claims({"access_token": "upstream-at"})
    assert claims == {"sub": "s2", "email": "owner@example.com", "email_verified": True}
    assert route.calls.last.request.headers["authorization"] == "Bearer upstream-at"


async def test_userinfo_completes_an_id_token_without_email(proxy: AllowlistOIDCProxy) -> None:
    tokens = {"access_token": "at", "id_token": id_token(sub="s3")}
    with respx.mock(assert_all_mocked=True) as router:
        router.get(USERINFO_URL).mock(
            return_value=httpx.Response(
                200, json={"sub": "s3", "email": "owner@example.com", "email_verified": True}
            )
        )
        claims = await proxy._extract_upstream_claims(tokens)
    assert claims == {"sub": "s3", "email": "owner@example.com", "email_verified": True}


async def test_userinfo_for_another_subject_is_ignored(proxy: AllowlistOIDCProxy) -> None:
    tokens = {"access_token": "at", "id_token": id_token(sub="s3")}
    with respx.mock(assert_all_mocked=True) as router:
        router.get(USERINFO_URL).mock(
            return_value=httpx.Response(
                200, json={"sub": "other", "email": "owner@example.com", "email_verified": True}
            )
        )
        claims = await proxy._extract_upstream_claims(tokens)
    assert claims == {"sub": "s3", "email": None, "email_verified": False}


async def test_userinfo_failure_gives_no_identity(proxy: AllowlistOIDCProxy) -> None:
    with respx.mock(assert_all_mocked=True) as router:
        router.get(USERINFO_URL).mock(return_value=httpx.Response(401))
        assert await proxy._extract_upstream_claims({"access_token": "at"}) is None


def _patch_parent_load(monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any] | None) -> None:
    async def load(self: OAuthProxy, token: str) -> AccessToken | None:
        from fastmcp.server.auth.auth import AccessToken as FastMCPAccessToken

        extra = {"upstream_claims": claims} if claims is not None else {}
        return FastMCPAccessToken(token="upstream", client_id="c", scopes=["openid"], claims=extra)

    monkeypatch.setattr(OAuthProxy, "load_access_token", load)


async def test_load_access_token_refuses_a_user_not_allowed(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_parent_load(
        monkeypatch, {"sub": "x", "email": "intruder@example.com", "email_verified": True}
    )
    assert await proxy.load_access_token("token") is None
    refusals = [r for r in caplog.records if "refused" in r.getMessage()]
    assert "intruder@example.com" in refusals[-1].user  # type: ignore[attr-defined]


async def test_load_access_token_refuses_unverified_email(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_parent_load(
        monkeypatch, {"sub": "x", "email": "owner@example.com", "email_verified": False}
    )
    assert await proxy.load_access_token("token") is None


async def test_load_access_token_refuses_tokens_without_identity(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_parent_load(monkeypatch, None)
    assert await proxy.load_access_token("token") is None


async def test_load_access_token_accepts_an_allowed_user(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_parent_load(
        monkeypatch, {"sub": "x", "email": "owner@example.com", "email_verified": True}
    )
    token = await proxy.load_access_token("token")
    assert token is not None
    assert token.client_id == "c"


async def test_load_access_token_keeps_invalid_tokens_invalid(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(self: OAuthProxy, token: str) -> None:
        return None

    monkeypatch.setattr(OAuthProxy, "load_access_token", load)
    assert await proxy.load_access_token("token") is None


# -- refusal at the end of the sign-in --------------------------------------------------

CLIENT_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


async def _callback_with(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch, idp_tokens: dict[str, Any]
) -> HTMLResponse | RedirectResponse:
    """Run our callback override over a parent that issued ``code-1`` for ``idp_tokens``."""
    await proxy._code_store.put(
        key="code-1",
        value=ClientCode(
            code="code-1",
            client_id="client",
            redirect_uri=CLIENT_REDIRECT,
            code_challenge="challenge",
            code_challenge_method="S256",
            scopes=["openid"],
            idp_tokens=idp_tokens,
            expires_at=time.time() + 300,
            created_at=time.time(),
        ),
    )

    async def parent(self: OAuthProxy, request: Request) -> RedirectResponse:
        return RedirectResponse(f"{CLIENT_REDIRECT}?code=code-1&state=client-state&iss=x", 302)

    monkeypatch.setattr(OAuthProxy, "_handle_idp_callback", parent)
    request = Request({"type": "http", "method": "GET", "path": "/auth/callback", "headers": []})
    return await proxy._handle_idp_callback(request)


async def test_callback_refuses_a_user_not_allowed(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = {
        "access_token": "at",
        "id_token": id_token(sub="x", email="intruder@example.com", email_verified=True),
    }
    response = await _callback_with(proxy, monkeypatch, tokens)
    location = urlsplit(response.headers["location"])
    params = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == CLIENT_REDIRECT
    assert params["error"] == ["access_denied"]
    assert params["state"] == ["client-state"]
    assert "code" not in params
    # The code can no longer be exchanged for a token.
    assert await proxy._code_store.get(key="code-1") is None


async def test_callback_lets_an_allowed_user_through(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = {
        "access_token": "at",
        "id_token": id_token(sub="x", email="owner@example.com", email_verified=True),
    }
    response = await _callback_with(proxy, monkeypatch, tokens)
    assert parse_qs(urlsplit(response.headers["location"]).query)["code"] == ["code-1"]
    assert await proxy._code_store.get(key="code-1") is not None


async def test_callback_passes_errors_through(
    proxy: AllowlistOIDCProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def parent(self: OAuthProxy, request: Request) -> HTMLResponse:
        return HTMLResponse("error", status_code=400)

    monkeypatch.setattr(OAuthProxy, "_handle_idp_callback", parent)
    request = Request({"type": "http", "method": "GET", "path": "/auth/callback", "headers": []})
    response = await proxy._handle_idp_callback(request)
    assert response.status_code == 400


def test_upstream_scopes_and_extra_params_reach_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_discovery(monkeypatch, tmp_path)
    proxy = build_auth(
        http_settings(
            upstream_scopes="openid email",
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        )
    )
    assert proxy._extra_authorize_params == {
        "access_type": "offline",
        "prompt": "consent",
        "scope": "openid email",
    }
