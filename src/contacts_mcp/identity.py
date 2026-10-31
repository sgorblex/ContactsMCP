"""Who is calling: the signed-in user in HTTP mode, ``local`` over stdio."""

from __future__ import annotations

from typing import Any

LOCAL_USER = "local"


def upstream_claims() -> dict[str, Any] | None:
    """The identity claims embedded in this request's access token, if any.

    Filled by the HTTP auth layer from the identity provider's ID token / userinfo
    (``email``, ``email_verified``, ``sub``).
    """
    from fastmcp.server.dependencies import get_access_token

    try:
        token = get_access_token()
    except RuntimeError:
        return None
    if token is None:
        return None
    claims = (token.claims or {}).get("upstream_claims")
    return claims if isinstance(claims, dict) else None


def current_user() -> str:
    """The verified e-mail, else the subject, of the signed-in user; ``local`` without auth."""
    claims = upstream_claims()
    if not claims:
        return LOCAL_USER
    return user_label(claims)


def user_label(claims: dict[str, Any]) -> str:
    """The verified e-mail, else the subject, in identity ``claims``."""
    email = claims.get("email")
    if isinstance(email, str) and claims.get("email_verified") is True:
        return email.lower()
    sub = claims.get("sub")
    return str(sub) if sub is not None else "unknown"
