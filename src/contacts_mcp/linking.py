"""The Google side of linking an account, for the auth CLI.

Authorization code flow with PKCE: :func:`authorization_url` sends the browser to Google's
consent screen, and :func:`complete_link` turns the code Google redirects back with into a
refresh token, after checking the grant and that the People API answers. :func:`revoke`
cancels a grant when an account is removed.

Nothing here prints or logs: failures raise :class:`LinkError`, whose message is shown to
the user as is and never holds a token, a code or the client secret.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from contacts_mcp.config import GoogleClientConfig
from contacts_mcp.fields import PEOPLE_API_URL, SCOPES
from contacts_mcp.google.oauth import AUTH_URL, REVOKE_URL, TOKEN_URL

#: Asked when linking: the contacts scopes, plus `openid email` so the account's own
#: address comes back in the ID token (`contacts` does not allow reading it from
#: people/me, which answers 403).
LINK_SCOPES = (*SCOPES, "openid", "email")
#: The ``iss`` of Google's ID tokens.
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

#: Failures a caller may want to explain in its own terms (which command to rerun, which
#: variables hold the client); ``None`` for the rest.
LinkErrorKind = Literal["invalid_grant", "redirect_uri_mismatch", "invalid_client"]
RevokeOutcome = Literal["revoked", "already_revoked", "failed"]


class LinkError(Exception):
    """Linking failed; the message is shown to the user as is (and holds no secret)."""

    def __init__(self, message: str, kind: LinkErrorKind | None = None) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str


@dataclass(frozen=True)
class Grant:
    """What a successful link yields: the refresh token, the scopes granted, the e-mail."""

    refresh_token: str
    scopes: list[str]
    email: str | None


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return Pkce(verifier, challenge)


def new_state() -> str:
    return secrets.token_urlsafe(32)


def state_matches(got: str, expected: str) -> bool:
    return secrets.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


def authorization_url(
    client: GoogleClientConfig,
    redirect_uri: str,
    state: str,
    challenge: str,
) -> str:
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(LINK_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def complete_link(
    http: httpx.AsyncClient,
    client: GoogleClientConfig,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> Grant:
    """Exchange ``code``, check the grant and the People API. Raises LinkError."""
    tokens = await _exchange_code(http, client, code, redirect_uri, verifier)
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise LinkError(
            "Google returned no refresh token. Remove this app's access at "
            "https://myaccount.google.com/permissions and link the account again."
        )
    scope_field = tokens.get("scope")
    granted = scope_field.split() if isinstance(scope_field, str) else []
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise LinkError(
            "Not every permission was granted. Link the account again and tick all the "
            "boxes on Google's consent screen (missing: " + ", ".join(missing) + ")."
        )
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise LinkError("Google returned no access token.")
    await _check_people_api(http, access_token)
    email = _email_from_id_token(tokens.get("id_token"), client)
    return Grant(refresh_token=refresh_token, scopes=granted, email=email)


async def revoke(http: httpx.AsyncClient, refresh_token: str) -> RevokeOutcome:
    """Revoke the grant behind ``refresh_token`` at Google. Never raises."""
    try:
        response = await http.post(REVOKE_URL, data={"token": refresh_token})
    except httpx.HTTPError:
        return "failed"
    if response.status_code == 200:
        return "revoked"
    if _json(response).get("error") == "invalid_token":
        return "already_revoked"
    return "failed"


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _exchange_code(
    http: httpx.AsyncClient,
    client: GoogleClientConfig,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> dict[str, Any]:
    try:
        response = await http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client.client_id,
                "client_secret": client.client_secret.get_secret_value(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
    except httpx.HTTPError as e:
        raise LinkError(f"could not reach Google ({type(e).__name__})") from None
    body = _json(response)
    if response.status_code == 200:
        return body
    error = body.get("error")
    error = error if isinstance(error, str) else ""
    if error == "invalid_grant":
        raise LinkError(
            "Google rejected the authorization code: it expired or was already used.",
            "invalid_grant",
        )
    if error == "redirect_uri_mismatch":
        raise LinkError(
            "Google rejected the redirect URI (redirect_uri_mismatch).", "redirect_uri_mismatch"
        )
    if error in ("invalid_client", "unauthorized_client"):
        raise LinkError(f"Google rejected the OAuth client ({error}).", "invalid_client")
    raise LinkError(
        f"the token exchange failed (HTTP {response.status_code}"
        + (f", {error}" if error else "")
        + ")"
    )


def _email_from_id_token(id_token: object, client: GoogleClientConfig) -> str | None:
    """The verified e-mail in the ID token Google returned from the token endpoint.

    It comes straight from Google over TLS in answer to our own code exchange, so its
    signature need not be checked (OpenID Connect Core 3.1.3.7); its issuer and audience
    still must be Google and this client. The address is only a label for the account,
    never used to authorize anything.
    """
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        return None
    payload = id_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if not isinstance(claims, dict) or claims.get("email_verified") is not True:
        return None
    if claims.get("iss") not in _GOOGLE_ISSUERS or claims.get("aud") != client.client_id:
        return None
    email = claims.get("email")
    return email if isinstance(email, str) and email else None


async def _check_people_api(http: httpx.AsyncClient, access_token: str) -> None:
    """Fail now, not at the first tool call, when the People API can't be used."""
    try:
        response = await http.get(
            f"{PEOPLE_API_URL}/people/me/connections",
            params={"pageSize": 1, "personFields": "names"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError as e:
        raise LinkError(f"could not reach the People API ({type(e).__name__})") from None
    if response.status_code == 403:
        reason = _google_reason(response)
        if reason == "SERVICE_DISABLED":
            raise LinkError(
                "the People API is not enabled in the Google Cloud project of the OAuth "
                "client: enable it (APIs & Services → Library → People API) and try again."
            )
        raise LinkError(
            "Google refused access to the contacts (403"
            + (f", {reason}" if reason else "")
            + "). For a Workspace account, an admin may have to allow this OAuth client."
        )
    if response.status_code != 200:
        raise LinkError(f"the People API check failed (HTTP {response.status_code})")


def _google_reason(response: httpx.Response) -> str | None:
    """The machine-readable reason of a Google API error (``SERVICE_DISABLED``, …)."""
    error = _json(response).get("error")
    if not isinstance(error, dict):
        return None
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
            return str(detail["reason"])
    status = error.get("status")
    return status if isinstance(status, str) else None
