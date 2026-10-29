"""Helpers for tests that build the OAuth provider: a fake OpenID provider and HTTP settings.

fastmcp fetches the discovery document with ``httpx2`` (not ``httpx``), which respx
does not intercept, so :func:`fake_discovery` patches that one call.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import fastmcp
import httpx2
import pytest
from fastmcp.server.auth import oidc_proxy
from pydantic import SecretStr

from contacts_mcp.config import DEFAULT_REDIRECT_URIS, HttpSettings, Settings

ISSUER = "https://auth.example.com/application/o/contacts/"
CONFIG_URL = ISSUER + ".well-known/openid-configuration"
USERINFO_URL = "https://auth.example.com/application/o/userinfo/"
CLIENT_ID = "oidc-client-id"
BASE_URL = "https://contacts.example.com"

DISCOVERY: dict[str, Any] = {
    "issuer": ISSUER,
    "authorization_endpoint": "https://auth.example.com/application/o/authorize/",
    "token_endpoint": "https://auth.example.com/application/o/token/",
    "userinfo_endpoint": USERINFO_URL,
    "jwks_uri": ISSUER + "jwks/",
    "response_types_supported": ["code"],
    "subject_types_supported": ["public"],
    "id_token_signing_alg_values_supported": ["RS256"],
}


def fake_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Serve :data:`DISCOVERY` at :data:`CONFIG_URL`, and keep OAuth state under tmp."""

    def get(url: str, **_: Any) -> httpx2.Response:
        request = httpx2.Request("GET", url)
        if url != CONFIG_URL:
            return httpx2.Response(404, request=request)
        return httpx2.Response(200, json=DISCOVERY, request=request)

    oidc_proxy._clear_oidc_configuration_cache()
    monkeypatch.setattr(httpx2, "get", get)
    monkeypatch.setattr(fastmcp.settings, "home", tmp_path / "fastmcp-home")


def http_settings(**overrides: Any) -> HttpSettings:
    values: dict[str, Any] = {
        "host": "0.0.0.0",  # noqa: S104
        "port": 8000,
        "base_url": BASE_URL,
        "oidc_config_url": CONFIG_URL,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": SecretStr("oidc-client-secret"),
        "jwt_signing_key": SecretStr("x" * 40),
        "allowed_users": frozenset({"owner@example.com", "sub-allowed"}),
        "allowed_redirect_uris": DEFAULT_REDIRECT_URIS,
        "upstream_scopes": "openid email offline_access",
        "trust_forwarded": False,
        "forwarded_allow_ips": "127.0.0.1",
    }
    values.update(overrides)
    return HttpSettings(**values)


def settings(
    *, transport: str = "http", exposed_tools: str = "all", data_dir: Path | None = None
) -> Settings:
    return Settings(
        transport=transport,
        server_name="contacts",
        data_dir=data_dir or Path("/nonexistent"),
        exposed_tools=exposed_tools,
        log_level="INFO",
        http=http_settings() if transport == "http" else None,
    )


def _b64(data: dict[str, Any]) -> str:
    raw = json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def id_token(**claims: Any) -> str:
    """An (unsigned) ID token as the provider's token endpoint would return it."""
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "exp": 2_000_000_000, **claims}
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(payload)}.c2lnbmF0dXJl"
