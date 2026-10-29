"""Configuration from environment variables, validated at startup.

Every error names the variable at fault. Each secret can also be given as
``<NAME>_FILE`` (a path, for Docker/Podman secrets); setting both is an error.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr

ENV_PREFIX = "CONTACTS_MCP_"

DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*/callback",
    "http://127.0.0.1:*/callback",
)
DEFAULT_UPSTREAM_SCOPES = "openid email offline_access"


class ConfigError(Exception):
    """A configuration problem; the message names the variable."""


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def read_secret(env: Mapping[str, str], name: str) -> str | None:
    """``NAME`` or the contents of the file at ``NAME_FILE``, never both."""
    direct = _get(env, name)
    path = _get(env, f"{name}_FILE")
    if direct is not None and path is not None:
        raise ConfigError(f"Set {name} or {name}_FILE, not both")
    if path is None:
        return direct
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ConfigError(f"{name}_FILE: cannot read {path}: {e.strerror}") from e
    if not value:
        raise ConfigError(f"{name}_FILE: {path} is empty")
    return value


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class GoogleClientConfig(BaseModel):
    """The Google Cloud OAuth client (type *Desktop app*) shared by every account."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    client_secret: SecretStr


def load_google_client(env: Mapping[str, str] | None = None) -> GoogleClientConfig:
    """From ``GOOGLE_CLIENT_ID``/``GOOGLE_CLIENT_SECRET`` or ``GOOGLE_CLIENT_SECRETS_FILE``."""
    env = os.environ if env is None else env
    client_id = read_secret(env, "GOOGLE_CLIENT_ID")
    client_secret = read_secret(env, "GOOGLE_CLIENT_SECRET")
    secrets_file = _get(env, "GOOGLE_CLIENT_SECRETS_FILE")

    if secrets_file is not None:
        if client_id is not None or client_secret is not None:
            raise ConfigError(
                "Set either GOOGLE_CLIENT_SECRETS_FILE or GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET, not both"
            )
        return _load_client_secrets_file(Path(secrets_file))

    if client_id is None and client_secret is None:
        raise ConfigError(
            "Google OAuth client not configured: set GOOGLE_CLIENT_SECRETS_FILE, or "
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
        )
    if client_id is None:
        raise ConfigError("GOOGLE_CLIENT_ID is required with GOOGLE_CLIENT_SECRET")
    if client_secret is None:
        raise ConfigError("GOOGLE_CLIENT_SECRET is required with GOOGLE_CLIENT_ID")
    return GoogleClientConfig(client_id=client_id, client_secret=SecretStr(client_secret))


def _load_client_secrets_file(path: Path) -> GoogleClientConfig:
    name = "GOOGLE_CLIENT_SECRETS_FILE"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"{name}: cannot read {path}: {e.strerror}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{name}: {path} is not valid JSON") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{name}: {path} is not a Google OAuth client file")
    if "web" in data:
        raise ConfigError(
            f"{name}: {path} is a 'Web application' client; create an OAuth client of "
            "type 'Desktop app' instead (the account-linking flow uses a loopback redirect)"
        )
    installed = data.get("installed")
    if not isinstance(installed, dict):
        raise ConfigError(f"{name}: {path} has no 'installed' section (Desktop app client)")
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise ConfigError(f"{name}: {path} lacks client_id or client_secret")
    return GoogleClientConfig(client_id=client_id, client_secret=SecretStr(client_secret))


class HttpSettings(BaseModel):
    """Settings needed only by the streamable HTTP transport (OAuth in front)."""

    model_config = ConfigDict(frozen=True)

    host: str
    port: int
    base_url: str
    oidc_config_url: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    jwt_signing_key: SecretStr
    allowed_users: frozenset[str]
    allowed_redirect_uris: tuple[str, ...]
    upstream_scopes: str
    #: Extra query parameters for the provider's authorization endpoint (e.g. Google's
    #: ``access_type=offline``); never ``scope``, which is ``upstream_scopes``.
    extra_authorize_params: dict[str, str] = Field(default_factory=dict)
    #: Verify the provider's ID token instead of its access token, for providers whose
    #: access tokens are opaque (not JWTs), e.g. Google.
    verify_id_token: bool = False
    trust_forwarded: bool
    forwarded_allow_ips: str

    @property
    def public_host(self) -> str:
        """The host name of ``base_url`` (no port), for Host-header checks."""
        return urlsplit(self.base_url).hostname or ""


class Settings(BaseModel):
    """Everything the server reads from the environment."""

    model_config = ConfigDict(frozen=True)

    transport: Literal["stdio", "http"]
    server_name: str
    data_dir: Path
    exposed_tools: str
    log_level: str
    http: HttpSettings | None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _get(env, name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, not {value!r}")


def default_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_DATA_HOME/contacts-mcp`` (``~/.local/share/contacts-mcp``) for local use."""
    env = os.environ if env is None else env
    base = _get(env, "XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / "contacts-mcp"


def load_data_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    value = _get(env, f"{ENV_PREFIX}DATA_DIR")
    return Path(value).expanduser() if value else default_data_dir(env)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and validate the server settings. Raises ConfigError."""
    env = os.environ if env is None else env
    p = ENV_PREFIX

    transport = (_get(env, f"{p}TRANSPORT") or "stdio").lower()
    if transport not in ("stdio", "http"):
        raise ConfigError(f"{p}TRANSPORT must be 'stdio' or 'http', not {transport!r}")

    server_name = _get(env, f"{p}SERVER_NAME") or "contacts"
    log_level = (_get(env, f"{p}LOG_LEVEL") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError(f"{p}LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, not {log_level!r}")

    http = _load_http_settings(env) if transport == "http" else None
    return Settings(
        transport="http" if transport == "http" else "stdio",
        server_name=server_name,
        data_dir=load_data_dir(env),
        exposed_tools=_get(env, f"{p}EXPOSED_TOOLS") or "all",
        log_level=log_level,
        http=http,
    )


def _require(env: Mapping[str, str], name: str, *, secret: bool = False) -> str:
    value = read_secret(env, name) if secret else _get(env, name)
    if value is None:
        hint = f" (or {name}_FILE)" if secret else ""
        raise ConfigError(f"{name}{hint} is required with {ENV_PREFIX}TRANSPORT=http")
    return value


def _load_http_settings(env: Mapping[str, str]) -> HttpSettings:
    p = ENV_PREFIX
    port_raw = _get(env, f"{p}PORT") or "8000"
    try:
        port = int(port_raw)
    except ValueError:
        raise ConfigError(f"{p}PORT must be a port number, not {port_raw!r}") from None
    if not 0 < port < 65536:
        raise ConfigError(f"{p}PORT must be between 1 and 65535")

    base_url = _require(env, f"{p}BASE_URL").rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ConfigError(f"{p}BASE_URL must be an http(s) URL, e.g. https://contacts.example.com")
    if not urlsplit(base_url).hostname:
        raise ConfigError(f"{p}BASE_URL has no host name")

    jwt_key = _require(env, f"{p}JWT_SIGNING_KEY", secret=True)
    if len(jwt_key) < 32:
        raise ConfigError(f"{p}JWT_SIGNING_KEY must be at least 32 characters")

    allowed_users = frozenset(
        u.lower() if "@" in u else u for u in _split_list(_get(env, f"{p}ALLOWED_USERS"))
    )
    if not allowed_users:
        raise ConfigError(
            f"{p}ALLOWED_USERS is required with {p}TRANSPORT=http: a comma-separated list "
            "of the e-mail addresses and/or OIDC subject IDs allowed to sign in"
        )

    redirect_uris = _split_list(_get(env, f"{p}ALLOWED_REDIRECT_URIS")) or list(
        DEFAULT_REDIRECT_URIS
    )
    return HttpSettings(
        host=_get(env, f"{p}HOST") or "0.0.0.0",  # noqa: S104 - container default
        port=port,
        base_url=base_url,
        oidc_config_url=_require(env, f"{p}OIDC_CONFIG_URL"),
        oidc_client_id=_require(env, f"{p}OIDC_CLIENT_ID", secret=True),
        oidc_client_secret=SecretStr(_require(env, f"{p}OIDC_CLIENT_SECRET", secret=True)),
        jwt_signing_key=SecretStr(jwt_key),
        allowed_users=allowed_users,
        allowed_redirect_uris=tuple(redirect_uris),
        upstream_scopes=_get(env, f"{p}OIDC_UPSTREAM_SCOPES") or DEFAULT_UPSTREAM_SCOPES,
        extra_authorize_params=_authorize_params(env, f"{p}OIDC_EXTRA_AUTHORIZE_PARAMS"),
        verify_id_token=_bool(env, f"{p}OIDC_VERIFY_ID_TOKEN", False),
        trust_forwarded=_bool(env, f"{p}TRUST_FORWARDED_HEADERS", False),
        forwarded_allow_ips=_get(env, f"{p}FORWARDED_ALLOW_IPS") or "127.0.0.1",
    )


def _authorize_params(env: Mapping[str, str], name: str) -> dict[str, str]:
    """``key=value&key=value`` (URL query syntax) → dict; ``scope`` is refused."""
    raw = _get(env, name)
    if raw is None:
        return {}
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ConfigError(
            f"{name} must look like key=value&key=value, e.g. access_type=offline&prompt=consent"
        ) from None
    params = dict(pairs)
    if "scope" in params:
        raise ConfigError(f"{name} must not set scope; use {ENV_PREFIX}OIDC_UPSTREAM_SCOPES")
    return params
