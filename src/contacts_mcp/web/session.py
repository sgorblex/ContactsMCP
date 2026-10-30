"""Signing in to the settings page: OIDC in the browser, and the sessions that follow.

The page signs its user in at the same OpenID provider as the MCP endpoint, with the same
client, through its own redirect URI (:data:`CALLBACK_PATH`): authorization code with PKCE
and ``state``, the state also held in a short-lived cookie so the callback must come back
to the browser that started it. The identity is read from the provider's token response
by the OAuth proxy, and only a user in ``CONTACTS_MCP_ALLOWED_USERS`` gets a session.

A session is a random token in the ``__Host-contacts_session`` cookie, kept in memory
only as its SHA-256: a restart signs everyone out. It ends after
:data:`IDLE_SECONDS` without use or :data:`MAX_SECONDS` in all.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from contacts_mcp.config import HttpSettings
from contacts_mcp.identity import user_label
from contacts_mcp.linking import new_pkce, new_state, state_matches
from contacts_mcp.oidc import AllowlistOIDCProxy, decode_jwt_payload, describe, is_allowed

log = logging.getLogger(__name__)

PREFIX = "/settings"
CALLBACK_PATH = f"{PREFIX}/auth/callback"
SESSION_COOKIE = "__Host-contacts_session"
SIGNIN_COOKIE = "__Host-contacts_signin"

IDLE_SECONDS = 30 * 60
MAX_SECONDS = 12 * 60 * 60
#: A removal needs a sign-in this recent.
STEP_UP_SECONDS = 10 * 60
#: How long a sign-in may take at the provider.
PENDING_SECONDS = 10 * 60
MAX_SESSIONS = 50
MAX_PENDING = 100
#: Clock skew allowed when comparing the provider's ``auth_time`` with ours.
SKEW_SECONDS = 60


@dataclass
class Session:
    claims: dict[str, Any]
    created: float
    last_seen: float
    #: When the user last proved who they are at the provider (for step-up).
    authenticated_at: float
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    #: One-shot message shown on the next page.
    notice: str | None = None

    @property
    def user(self) -> str:
        return user_label(self.claims)

    def is_fresh(self, now: float) -> bool:
        return now - self.authenticated_at <= STEP_UP_SECONDS


@dataclass
class PendingSignIn:
    verifier: str
    return_to: str
    step_up: bool
    created: float


class Expiring[T]:
    """A small map whose entries expire, oldest dropped first beyond ``limit``."""

    def __init__(self, ttl: float, limit: int) -> None:
        self.ttl = ttl
        self.limit = limit
        self._items: OrderedDict[str, tuple[float, T]] = OrderedDict()

    def put(self, key: str, value: T) -> None:
        self._items[key] = (time.time(), value)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def pop(self, key: str) -> T | None:
        item = self._items.pop(key, None)
        if item is None or time.time() - item[0] > self.ttl:
            return None
        return item[1]


class Sessions:
    """Sessions by the SHA-256 of their cookie token."""

    def __init__(self) -> None:
        self._by_hash: OrderedDict[str, Session] = OrderedDict()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, claims: dict[str, Any], authenticated_at: float) -> tuple[str, Session]:
        now = time.time()
        token = secrets.token_urlsafe(32)
        session = Session(claims, now, now, authenticated_at)
        self._by_hash[self._hash(token)] = session
        while len(self._by_hash) > MAX_SESSIONS:
            self._by_hash.popitem(last=False)
        return token, session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        key = self._hash(token)
        session = self._by_hash.get(key)
        if session is None:
            return None
        now = time.time()
        if now - session.last_seen > IDLE_SECONDS or now - session.created > MAX_SECONDS:
            del self._by_hash[key]
            return None
        session.last_seen = now
        return session

    def delete(self, token: str | None) -> None:
        if token:
            self._by_hash.pop(self._hash(token), None)


class SignInError(Exception):
    """Signing in failed; the message is shown on the page (and holds no secret)."""


class BrowserSignIn:
    """The OIDC sign-in of the settings page, and the sessions it opens."""

    def __init__(
        self, http_settings: HttpSettings, auth: AllowlistOIDCProxy, http: httpx.AsyncClient
    ) -> None:
        self.settings = http_settings
        self.auth = auth
        self.http = http
        self.sessions = Sessions()
        self._pending: Expiring[PendingSignIn] = Expiring(PENDING_SECONDS, MAX_PENDING)
        self.redirect_uri = http_settings.base_url + CALLBACK_PATH

    @property
    def provider_origin(self) -> str:
        """Where sign-in redirects go, for the pages' ``form-action``."""
        parts = urlsplit(str(self.auth.oidc_config.authorization_endpoint))
        return f"{parts.scheme}://{parts.netloc}"

    def session(self, request: Request) -> Session | None:
        session = self.sessions.get(request.cookies.get(SESSION_COOKIE))
        if session is not None and not is_allowed(session.claims, self.settings.allowed_users):
            return None
        return session

    def start(self, return_to: str, *, step_up: bool = False) -> RedirectResponse:
        """Send the browser to the provider; ``step_up`` makes it ask for the login again."""
        if not return_to.startswith(PREFIX):
            return_to = PREFIX
        pkce = new_pkce()
        state = new_state()
        self._pending.put(state, PendingSignIn(pkce.verifier, return_to, step_up, time.time()))
        params = {
            "client_id": self.settings.oidc_client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
        }
        if step_up:
            # Not prompt=login, which Google refuses; not max_age=0, which some providers
            # (authentik) read as "no limit"; not a tiny max_age, which a provider may apply
            # again right after the login and loop. A login it skips as recent enough is
            # within SKEW_SECONDS, which finish() accepts.
            params["max_age"] = str(SKEW_SECONDS)
        endpoint = str(self.auth.oidc_config.authorization_endpoint)
        separator = "&" if "?" in endpoint else "?"
        response = RedirectResponse(f"{endpoint}{separator}{urlencode(params)}", status_code=303)
        # Lax, not Strict: the provider's redirect back is a cross-site navigation.
        response.set_cookie(
            SIGNIN_COOKIE,
            state,
            max_age=PENDING_SECONDS,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    async def finish(self, request: Request, response: Response) -> str:
        """Handle the provider's redirect: open a session on ``response``, return where to go.

        Raises SignInError.
        """
        params = request.query_params
        state = params.get("state", "")
        cookie_state = request.cookies.get(SIGNIN_COOKIE, "")
        if not state or not cookie_state or not state_matches(state, cookie_state):
            raise SignInError("This sign-in did not start in this browser. Sign in again.")
        pending = self._pending.pop(state)
        if pending is None:
            raise SignInError("This sign-in expired. Sign in again.")
        if params.get("error"):
            raise SignInError(f"The identity provider refused: {params['error'][:100]}")
        code = params.get("code")
        if not code:
            raise SignInError("The identity provider sent no authorization code.")

        tokens = await self._exchange(code, pending.verifier)
        claims = await self.auth._extract_upstream_claims(tokens)
        if not is_allowed(claims, self.settings.allowed_users):
            log.warning(
                "settings sign-in refused: not in the allowlist", extra={"user": describe(claims)}
            )
            raise SignInError("This account is not allowed to use this server.")
        assert claims is not None

        now = time.time()
        auth_time = (decode_jwt_payload(str(tokens.get("id_token") or "")) or {}).get("auth_time")
        # Without auth_time nothing says when the user last logged in: never fresh.
        authenticated_at = min(now, float(auth_time)) if isinstance(auth_time, int | float) else 0.0
        if pending.step_up and not authenticated_at:
            raise SignInError(
                "The identity provider did not say when you signed in (no auth_time in the "
                "ID token), so this change cannot be confirmed."
            )
        if pending.step_up and authenticated_at < pending.created - SKEW_SECONDS:
            raise SignInError(
                "The identity provider did not ask you to sign in again, so this change "
                "cannot be confirmed. Sign out of the provider and try again."
            )

        self.sessions.delete(request.cookies.get(SESSION_COOKIE))
        token, _ = self.sessions.create(claims, authenticated_at)
        response.delete_cookie(SIGNIN_COOKIE, path="/", secure=True, httponly=True)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=MAX_SECONDS,
            path="/",
            secure=True,
            httponly=True,
            # Lax: the provider's redirect back must carry it.
            samesite="lax",
        )
        log.info("settings sign-in", extra={"user": describe(claims)})
        return pending.return_to

    def sign_out(self, request: Request, response: Response) -> None:
        self.sessions.delete(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)

    async def _exchange(self, code: str, verifier: str) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }
        client_id = self.settings.oidc_client_id
        client_secret = self.settings.oidc_client_secret.get_secret_value()
        methods = self.auth.oidc_config.token_endpoint_auth_methods_supported
        headers = {}
        if methods and "client_secret_basic" not in methods:
            data |= {"client_id": client_id, "client_secret": client_secret}
        else:
            # RFC 6749 2.3.1: both are form-encoded before the Basic encoding.
            basic = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
        try:
            response = await self.http.post(
                str(self.auth.oidc_config.token_endpoint), data=data, headers=headers
            )
        except httpx.HTTPError as e:
            raise SignInError(
                f"Could not reach the identity provider ({type(e).__name__})."
            ) from None
        if response.status_code != 200:
            log.warning("settings sign-in: token endpoint answered HTTP %s", response.status_code)
            raise SignInError("The identity provider refused the sign-in. Sign in again.")
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise SignInError("The identity provider's answer was unreadable.")
        return body
