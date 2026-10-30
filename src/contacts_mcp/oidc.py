"""OAuth for the HTTP transport: fastmcp's OIDCProxy plus this server's user allowlist.

MCP clients (claude.ai, Claude Code) register dynamically with this server and the
user signs in at the configured OpenID Connect provider. Every request to the MCP
endpoint then needs a bearer token this server issued. Client registrations and
upstream tokens are kept under ``FASTMCP_HOME``, encrypted with a key derived from
``CONTACTS_MCP_JWT_SIGNING_KEY``.

Signing in at the provider is not enough: with a public provider such as Google,
anyone with an account could. :class:`AllowlistOIDCProxy` embeds the user's identity
(``sub``, ``email``, ``email_verified``) in the tokens it issues and checks it against
``CONTACTS_MCP_ALLOWED_USERS`` at the end of the sign-in (a refused user never gets a
token) and again on every request (so shrinking the allowlist and restarting locks
out tokens issued earlier).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Mapping, Set
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.redirect_validation import build_client_redirect
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from contacts_mcp.config import HttpSettings

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
USERINFO_TIMEOUT = 10.0

#: The identity claims embedded in issued tokens (``upstream_claims``), read back by
#: :mod:`contacts_mcp.identity`.
IDENTITY_CLAIMS = ("sub", "email", "email_verified")

REFUSED_DESCRIPTION = "This account is not allowed to use this server."


def is_allowed(claims: Mapping[str, Any] | None, allowed_users: Set[str]) -> bool:
    """Whether the identity in ``claims`` is on the allowlist.

    ``allowed_users`` holds lowercased e-mail addresses and raw ``sub`` values. An
    entry containing ``@`` is an e-mail: it matches (case-insensitively) only a
    verified ``email`` claim (``email_verified`` true), never a ``sub``, since some
    providers use the e-mail, verified or not, as the subject. Other entries match
    ``sub`` exactly. Otherwise anyone able to put an arbitrary address on their
    account at the provider would get in.
    """
    if not claims or not allowed_users:
        return False
    sub = claims.get("sub")
    if isinstance(sub, str) and sub and "@" not in sub and sub in allowed_users:
        return True
    email = claims.get("email")
    return (
        isinstance(email, str)
        # Only an address may match an e-mail entry, never a subject entry.
        and "@" in email
        and claims.get("email_verified") is True
        and email.strip().lower() in allowed_users
    )


def describe(claims: Mapping[str, Any] | None) -> str:
    """A short identity string for logs (an e-mail or a subject, never a token)."""
    if not claims:
        return "unknown identity"
    email = claims.get("email")
    verified = "verified" if claims.get("email_verified") is True else "unverified"
    if isinstance(email, str) and email:
        return f"{email} ({verified}, sub {claims.get('sub')})"
    return f"sub {claims.get('sub')}"


def _verified_flag(value: Any) -> bool:
    """``email_verified`` as a bool (some providers send the string ``"true"``)."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def _normalize(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    sub = raw.get("sub")
    if sub is None or str(sub) == "":
        return None
    email = raw.get("email")
    return {
        "sub": str(sub),
        "email": email.strip() if isinstance(email, str) and email.strip() else None,
        "email_verified": _verified_flag(raw.get("email_verified")),
    }


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """The payload of a compact JWS, *without* checking its signature (see the caller)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment))
    except binascii.Error, ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class AllowlistOIDCProxy(OIDCProxy):
    """``OIDCProxy`` that records who signed in and refuses anyone not allowed."""

    def __init__(self, *, allowed_users: Set[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.allowed_users = frozenset(allowed_users)
        self._oidc_client_id: str = kwargs["client_id"]

    # -- identity ------------------------------------------------------------------

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        """``sub``, ``email`` and ``email_verified`` of the signed-in user.

        From the ID token when there is one, completed from the userinfo endpoint when
        it lacks the e-mail or there is no ID token. fastmcp embeds the result, signed,
        in the tokens it issues; it calls this after the code exchange and after each
        refresh (whose response may lack an ID token: fastmcp then passes the stored
        token data merged with the refresh response, so the original ID token is still
        there, and the userinfo endpoint covers the rest).
        """
        claims = self._id_token_claims(idp_tokens.get("id_token"))
        if claims is None or claims.get("email") is None:
            info = await self._userinfo(idp_tokens.get("access_token"))
            if info is not None:
                if claims is None:
                    claims = info
                elif info["sub"] != claims["sub"]:
                    # OIDC Core 5.3.2: a userinfo response for another subject is ignored.
                    log.warning("userinfo subject differs from the ID token's; ignored")
                else:
                    claims = {
                        **claims,
                        "email": info["email"],
                        "email_verified": info["email_verified"],
                    }
        if claims is None:
            log.warning("could not determine the signed-in user (no ID token, no userinfo)")
        return claims

    def _id_token_claims(self, id_token: Any) -> dict[str, Any] | None:
        """Identity claims of the ID token from the provider's token endpoint.

        The signature is not checked. The ID token is read only from the token
        endpoint's response, which this server received directly from the provider
        over TLS (OIDC Core 3.1.3.7, item 6 allows TLS server validation in place of
        the signature in that case), and is stored encrypted afterwards. Checking it
        against the JWKS would also reject the stale-but-genuine ID token kept after
        a refresh that returned none, for no gain. Issuer and audience are still
        compared, as a guard against a misconfiguration.
        """
        if not isinstance(id_token, str) or not id_token:
            return None
        payload = decode_jwt_payload(id_token)
        if payload is None:
            log.warning("the provider's ID token is not a readable JWT; ignored")
            return None
        expected_issuer = str(self.oidc_config.issuer or "").rstrip("/")
        if expected_issuer and str(payload.get("iss", "")).rstrip("/") != expected_issuer:
            log.warning("the ID token's issuer is not the configured provider's; ignored")
            return None
        aud = payload.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self._oidc_client_id not in audiences:
            log.warning("the ID token was not issued to this server's client ID; ignored")
            return None
        return _normalize(payload)

    async def _userinfo(self, access_token: Any) -> dict[str, Any] | None:
        endpoint = self.oidc_config.userinfo_endpoint
        if not endpoint or not isinstance(access_token, str) or not access_token:
            return None
        try:
            async with httpx.AsyncClient(timeout=USERINFO_TIMEOUT) as client:
                response = await client.get(
                    str(endpoint), headers={"Authorization": f"Bearer {access_token}"}
                )
        except httpx.HTTPError as e:
            log.warning("userinfo request failed: %s", type(e).__name__)
            return None
        if response.status_code != 200:
            log.warning("userinfo request failed: HTTP %s", response.status_code)
            return None
        try:
            body = response.json()
        except ValueError:
            # A signed (application/jwt) userinfo response is not supported.
            log.warning("userinfo response is not JSON")
            return None
        return _normalize(body) if isinstance(body, dict) else None

    # -- enforcement ---------------------------------------------------------------

    async def _handle_idp_callback(self, request: Request) -> HTMLResponse | RedirectResponse:
        """The provider's redirect back to us: refuse a user who is not allowed.

        fastmcp exchanges the provider's code and, on success, redirects the browser to
        the MCP client with a one-time code of ours. Before that redirect leaves, look
        up who signed in; if they are not allowed, delete the code (so it can never be
        exchanged for a token) and send the client ``error=access_denied`` instead
        (RFC 6749 4.1.2.1; the client's redirect URI was already validated).
        """
        response = await super()._handle_idp_callback(request)
        if not isinstance(response, RedirectResponse):
            return response
        location = response.headers.get("location", "")
        params = dict(parse_qsl(urlsplit(location).query))
        code = params.get("code")
        if not code:
            return response
        code_model = await self._code_store.get(key=code)
        if code_model is None:
            return response
        claims = await self._extract_upstream_claims(code_model.idp_tokens)
        if is_allowed(claims, self.allowed_users):
            log.info("sign-in allowed", extra={"user": describe(claims)})
            return response
        await self._code_store.delete(key=code)
        log.warning("sign-in refused: not in the allowlist", extra={"user": describe(claims)})
        error_params = {"error": "access_denied", "error_description": REFUSED_DESCRIPTION}
        if "state" in params:
            error_params["state"] = params["state"]
        # Keep the response (and its consent-cookie cleanup); only change where it goes.
        response.headers["location"] = build_client_redirect(
            code_model.redirect_uri, error_params, iss=str(self.issuer_url)
        )
        return response

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        """fastmcp's validation, then the allowlist: ``None`` (→ 401) unless allowed."""
        access = await super().load_access_token(token)
        if access is None:
            return None
        claims = (getattr(access, "claims", None) or {}).get("upstream_claims")
        if not is_allowed(claims if isinstance(claims, dict) else None, self.allowed_users):
            log.warning(
                "request refused: user not in the allowlist",
                extra={"user": describe(claims if isinstance(claims, dict) else None)},
            )
            return None
        return access


def build_auth(http: HttpSettings) -> AllowlistOIDCProxy:
    """The OAuth provider for the HTTP transport, from the validated settings.

    Fetches the provider's discovery document (network).
    """
    return AllowlistOIDCProxy(
        allowed_users=http.allowed_users,
        config_url=http.oidc_config_url,
        client_id=http.oidc_client_id,
        client_secret=http.oidc_client_secret.get_secret_value(),
        base_url=http.base_url,
        jwt_signing_key=http.jwt_signing_key.get_secret_value(),
        allowed_client_redirect_uris=list(http.allowed_redirect_uris),
        required_scopes=["openid"],
        verify_id_token=http.verify_id_token,
        # Asked of the provider whatever the client requests. Without offline_access the
        # provider issues no refresh token, so the client must sign in again each time
        # the access token expires (authentik: 5 minutes by default). `email` is what
        # the allowlist matches on (with `sub`).
        extra_authorize_params={**http.extra_authorize_params, "scope": http.upstream_scopes},
    )
