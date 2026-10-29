"""Google OAuth endpoints and the per-account access-token provider.

Access tokens live in memory only. Refresh is a direct POST to the token
endpoint over the shared ``httpx.AsyncClient``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

import httpx

from contacts_mcp.config import GoogleClientConfig
from contacts_mcp.errors import GoogleApiError, NeedsRelinkError

AUTH_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
REVOKE_URL: Final = "https://oauth2.googleapis.com/revoke"

#: Refresh this long before Google's stated expiry.
EXPIRY_MARGIN: Final = 60.0
REFRESH_ATTEMPTS: Final = 3

log = logging.getLogger(__name__)


class TokenProvider:
    """Hands out a valid access token for one account, refreshing when needed.

    Concurrent callers share a single refresh (an ``asyncio.Lock``). A refresh that
    Google answers with ``invalid_grant`` sets :attr:`invalid` and raises
    :class:`contacts_mcp.errors.NeedsRelinkError`; later calls raise it again without
    contacting Google. Other failures raise :class:`contacts_mcp.errors.GoogleApiError`
    with a short message (never the token or the response body).
    """

    def __init__(
        self,
        alias: str,
        client: GoogleClientConfig,
        refresh_token: str,
        http: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.alias = alias
        self._sleep = sleep
        self.invalid = False
        self._client = client
        self._refresh_token = refresh_token
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0
        #: Counts successful refreshes, so a forced refresh can tell that another caller
        #: already refreshed while it waited for the lock (token strings may repeat).
        self._generation = 0
        self._lock = asyncio.Lock()

    async def access_token(self, *, force_refresh: bool = False) -> str:
        """A token valid for at least another minute; ``force_refresh`` after a 401."""
        if self.invalid:
            raise NeedsRelinkError(self.alias)
        seen = self._generation
        async with self._lock:
            if self.invalid:
                raise NeedsRelinkError(self.alias)
            fresh = self._token is not None and time.monotonic() < self._expires_at
            # Another caller may have refreshed while we waited for the lock.
            if fresh and not (force_refresh and self._generation == seen):
                assert self._token is not None
                return self._token
            return await self._refresh()

    async def _refresh(self) -> str:
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client.client_id,
            "client_secret": self._client.client_secret.get_secret_value(),
            "refresh_token": self._refresh_token,
        }
        response: httpx.Response | None = None
        for attempt in range(REFRESH_ATTEMPTS):
            try:
                response = await self._http.post(TOKEN_URL, data=data)
            except httpx.TransportError as e:
                log.warning(
                    "token refresh failed: %s", type(e).__name__, extra={"account": self.alias}
                )
                response = None
            else:
                if response.status_code < 500:
                    break
            if attempt + 1 < REFRESH_ATTEMPTS:
                await self._sleep(0.5 * 2**attempt)
        if response is None:
            raise GoogleApiError(f"Could not reach Google to refresh the token of '{self.alias}'.")

        body = _json(response)
        token = body.get("access_token")
        if response.status_code == 200 and isinstance(token, str):
            self._token = token
            self._generation += 1
            expires_in = body.get("expires_in", 3600)
            if not isinstance(expires_in, int | float):
                expires_in = 3600
            self._expires_at = time.monotonic() + max(0.0, float(expires_in) - EXPIRY_MARGIN)
            return token

        error = body.get("error")
        if error == "invalid_grant":
            self.invalid = True
            self._token = None
            log.warning("refresh token rejected (invalid_grant)", extra={"account": self.alias})
            raise NeedsRelinkError(self.alias)
        if error in ("invalid_client", "unauthorized_client"):
            raise GoogleApiError(
                f"Google rejected the OAuth client ({error}) while refreshing '{self.alias}': "
                "check GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (the client the account was "
                "linked with).",
                status=response.status_code,
                reason=error,
            )
        raise GoogleApiError(
            f"Token refresh for '{self.alias}' failed (HTTP {response.status_code}"
            + (f", {error}" if isinstance(error, str) else "")
            + ").",
            status=response.status_code,
            reason=error if isinstance(error, str) else None,
        )


def _json(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
