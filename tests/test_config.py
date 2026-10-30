"""Configuration from the environment: Google client, HTTP settings, secrets from files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from contacts_mcp.config import (
    DEFAULT_REDIRECT_URIS,
    ConfigError,
    load_data_dir,
    load_google_client,
    load_google_web_client,
    load_settings,
)

# -- Google OAuth client ----------------------------------------------------------------


def test_google_client_from_id_and_secret() -> None:
    client = load_google_client({"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret"})
    assert client.client_id == "id"
    assert client.client_secret.get_secret_value() == "secret"


def test_google_client_secret_from_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("from-file\n")
    client = load_google_client(
        {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET_FILE": str(secret)}
    )
    assert client.client_secret.get_secret_value() == "from-file"


def _secrets_file(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(data))
    return path


def test_google_client_from_desktop_app_json(tmp_path: Path) -> None:
    path = _secrets_file(
        tmp_path, {"installed": {"client_id": "id.apps", "client_secret": "s", "redirect_uris": []}}
    )
    client = load_google_client({"GOOGLE_CLIENT_SECRETS_FILE": str(path)})
    assert client.client_id == "id.apps"
    assert client.client_secret.get_secret_value() == "s"


def test_no_web_client_by_default() -> None:
    assert load_google_web_client({}) is None


def test_web_client_from_id_and_secret() -> None:
    client = load_google_web_client(
        {"GOOGLE_WEB_CLIENT_ID": "web-id", "GOOGLE_WEB_CLIENT_SECRET": "web-secret"}
    )
    assert client is not None
    assert client.client_id == "web-id"
    assert client.client_secret.get_secret_value() == "web-secret"


def test_web_client_from_json(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, {"web": {"client_id": "web.apps", "client_secret": "s"}})
    client = load_google_web_client({"GOOGLE_WEB_CLIENT_SECRETS_FILE": str(path)})
    assert client is not None
    assert client.client_id == "web.apps"


def test_desktop_json_as_the_web_client_is_rejected(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, {"installed": {"client_id": "id", "client_secret": "s"}})
    with pytest.raises(ConfigError, match=r"'Desktop app' client.*'Web application'"):
        load_google_web_client({"GOOGLE_WEB_CLIENT_SECRETS_FILE": str(path)})


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"GOOGLE_WEB_CLIENT_ID": "id"}, "GOOGLE_WEB_CLIENT_SECRET is required"),
        ({"GOOGLE_WEB_CLIENT_SECRET": "s"}, "GOOGLE_WEB_CLIENT_ID is required"),
    ],
)
def test_incomplete_web_client(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_google_web_client(env)


def test_web_client_json_is_rejected(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, {"web": {"client_id": "id", "client_secret": "s"}})
    with pytest.raises(ConfigError, match=r"'Web application' client.*'Desktop app'"):
        load_google_client({"GOOGLE_CLIENT_SECRETS_FILE": str(path)})


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"other": {}}, "no 'installed' section"),
        ({"installed": {"client_id": "id"}}, "lacks client_id or client_secret"),
        ([1, 2], "not a Google OAuth client file"),
    ],
)
def test_bad_client_json(tmp_path: Path, data: Any, message: str) -> None:
    path = _secrets_file(tmp_path, data)
    with pytest.raises(ConfigError, match=message):
        load_google_client({"GOOGLE_CLIENT_SECRETS_FILE": str(path)})


def test_client_json_not_json(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("{nope")
    with pytest.raises(ConfigError, match=r"GOOGLE_CLIENT_SECRETS_FILE.*not valid JSON"):
        load_google_client({"GOOGLE_CLIENT_SECRETS_FILE": str(path)})


def test_client_json_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="GOOGLE_CLIENT_SECRETS_FILE: cannot read"):
        load_google_client({"GOOGLE_CLIENT_SECRETS_FILE": str(tmp_path / "missing.json")})


def test_both_credential_styles_is_an_error(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, {"installed": {"client_id": "id", "client_secret": "s"}})
    env = {
        "GOOGLE_CLIENT_SECRETS_FILE": str(path),
        "GOOGLE_CLIENT_ID": "id",
        "GOOGLE_CLIENT_SECRET": "s",
    }
    with pytest.raises(ConfigError, match="not both"):
        load_google_client(env)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "Google OAuth client not configured"),
        ({"GOOGLE_CLIENT_ID": "id"}, "GOOGLE_CLIENT_SECRET is required"),
        ({"GOOGLE_CLIENT_SECRET": "s"}, "GOOGLE_CLIENT_ID is required"),
        (
            {
                "GOOGLE_CLIENT_ID": "id",
                "GOOGLE_CLIENT_SECRET": "s",
                "GOOGLE_CLIENT_SECRET_FILE": "/x",
            },
            "Set GOOGLE_CLIENT_SECRET or GOOGLE_CLIENT_SECRET_FILE, not both",
        ),
    ],
)
def test_incomplete_google_client(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_google_client(env)


def test_empty_secret_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(ConfigError, match=r"GOOGLE_CLIENT_SECRET_FILE: .* is empty"):
        load_google_client({"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET_FILE": str(empty)})


# -- server settings --------------------------------------------------------------------


def test_stdio_defaults(tmp_path: Path) -> None:
    s = load_settings({"CONTACTS_MCP_DATA_DIR": str(tmp_path)})
    assert s.transport == "stdio"
    assert s.server_name == "contacts"
    assert s.exposed_tools == "all"
    assert s.log_level == "INFO"
    assert s.data_dir == tmp_path
    assert s.http is None


def test_data_dir_defaults_under_xdg_data_home_and_expands_the_home() -> None:
    assert load_data_dir({"XDG_DATA_HOME": "/srv/xdg"}) == Path("/srv/xdg/contacts-mcp")
    assert load_data_dir({"CONTACTS_MCP_DATA_DIR": "~/c"}) == Path.home() / "c"


def test_stdio_needs_no_http_variables() -> None:
    assert load_settings({"CONTACTS_MCP_TRANSPORT": "STDIO"}).http is None


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"CONTACTS_MCP_TRANSPORT": "sse"}, "CONTACTS_MCP_TRANSPORT must be 'stdio' or 'http'"),
        ({"CONTACTS_MCP_LOG_LEVEL": "loud"}, "CONTACTS_MCP_LOG_LEVEL"),
    ],
)
def test_bad_general_settings(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_settings(env)


HTTP_ENV = {
    "CONTACTS_MCP_TRANSPORT": "http",
    "CONTACTS_MCP_BASE_URL": "https://contacts.example.com/",
    "CONTACTS_MCP_OIDC_CONFIG_URL": "https://auth.example.com/.well-known/openid-configuration",
    "CONTACTS_MCP_OIDC_CLIENT_ID": "cid",
    "CONTACTS_MCP_OIDC_CLIENT_SECRET": "csecret",
    "CONTACTS_MCP_JWT_SIGNING_KEY": "k" * 32,
    "CONTACTS_MCP_ALLOWED_USERS": "Owner@Example.com, 1234567890 ,",
}


def test_http_settings() -> None:
    s = load_settings(HTTP_ENV)
    assert s.transport == "http"
    http = s.http
    assert http is not None
    assert http.host == "0.0.0.0"  # noqa: S104
    assert http.port == 8000
    assert http.base_url == "https://contacts.example.com"
    assert http.public_host == "contacts.example.com"
    assert http.allowed_users == frozenset({"owner@example.com", "1234567890"})
    assert http.allowed_redirect_uris == DEFAULT_REDIRECT_URIS
    assert http.upstream_scopes == "openid email offline_access"
    assert http.extra_authorize_params == {}
    assert http.verify_id_token is False
    assert http.trust_forwarded is False
    assert http.settings_page is False
    assert http.oidc_client_secret.get_secret_value() == "csecret"


def test_settings_page_flag() -> None:
    s = load_settings({**HTTP_ENV, "CONTACTS_MCP_SETTINGS_PAGE": "true"})
    assert s.http is not None
    assert s.http.settings_page is True


def test_settings_page_needs_https() -> None:
    env = {**HTTP_ENV, "CONTACTS_MCP_SETTINGS_PAGE": "true"}
    env["CONTACTS_MCP_BASE_URL"] = "http://contacts.example.com"
    with pytest.raises(ConfigError, match="needs an https CONTACTS_MCP_BASE_URL"):
        load_settings(env)


def test_settings_page_needs_http() -> None:
    with pytest.raises(ConfigError, match="CONTACTS_MCP_SETTINGS_PAGE needs"):
        load_settings({"CONTACTS_MCP_SETTINGS_PAGE": "true"})


@pytest.mark.parametrize(
    "name",
    [
        "CONTACTS_MCP_BASE_URL",
        "CONTACTS_MCP_OIDC_CONFIG_URL",
        "CONTACTS_MCP_OIDC_CLIENT_ID",
        "CONTACTS_MCP_OIDC_CLIENT_SECRET",
        "CONTACTS_MCP_JWT_SIGNING_KEY",
        "CONTACTS_MCP_ALLOWED_USERS",
    ],
)
def test_http_refuses_to_start_without(name: str) -> None:
    env = {k: v for k, v in HTTP_ENV.items() if k != name}
    with pytest.raises(ConfigError, match=name) as e:
        load_settings(env)
    assert "CONTACTS_MCP_TRANSPORT=http" in str(e.value)


def test_http_refuses_an_allowlist_of_blanks() -> None:
    with pytest.raises(ConfigError, match="CONTACTS_MCP_ALLOWED_USERS"):
        load_settings({**HTTP_ENV, "CONTACTS_MCP_ALLOWED_USERS": " , ,"})


def test_http_secrets_from_files(tmp_path: Path) -> None:
    (tmp_path / "oidc").write_text("oidc-from-file\n")
    (tmp_path / "jwt").write_text("j" * 40)
    env = {k: v for k, v in HTTP_ENV.items() if k not in ("CONTACTS_MCP_OIDC_CLIENT_SECRET",)}
    env.pop("CONTACTS_MCP_JWT_SIGNING_KEY")
    env["CONTACTS_MCP_OIDC_CLIENT_SECRET_FILE"] = str(tmp_path / "oidc")
    env["CONTACTS_MCP_JWT_SIGNING_KEY_FILE"] = str(tmp_path / "jwt")
    http = load_settings(env).http
    assert http is not None
    assert http.oidc_client_secret.get_secret_value() == "oidc-from-file"
    assert http.jwt_signing_key.get_secret_value() == "j" * 40


def test_secret_and_file_both_set_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "jwt").write_text("j" * 40)
    env = {**HTTP_ENV, "CONTACTS_MCP_JWT_SIGNING_KEY_FILE": str(tmp_path / "jwt")}
    with pytest.raises(ConfigError, match=r"CONTACTS_MCP_JWT_SIGNING_KEY or .*_FILE, not both"):
        load_settings(env)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"CONTACTS_MCP_JWT_SIGNING_KEY": "short"}, "at least 32 characters"),
        ({"CONTACTS_MCP_PORT": "http"}, "CONTACTS_MCP_PORT must be a port number"),
        ({"CONTACTS_MCP_PORT": "70000"}, "CONTACTS_MCP_PORT must be between"),
        ({"CONTACTS_MCP_BASE_URL": "contacts.example.com"}, "CONTACTS_MCP_BASE_URL must be"),
        ({"CONTACTS_MCP_TRUST_FORWARDED_HEADERS": "maybe"}, "TRUST_FORWARDED_HEADERS"),
        ({"CONTACTS_MCP_OIDC_VERIFY_ID_TOKEN": "maybe"}, "CONTACTS_MCP_OIDC_VERIFY_ID_TOKEN"),
        (
            {"CONTACTS_MCP_OIDC_EXTRA_AUTHORIZE_PARAMS": "scope=openid"},
            "must not set scope; use CONTACTS_MCP_OIDC_UPSTREAM_SCOPES",
        ),
        (
            {"CONTACTS_MCP_OIDC_EXTRA_AUTHORIZE_PARAMS": "access_type"},
            "CONTACTS_MCP_OIDC_EXTRA_AUTHORIZE_PARAMS must look like key=value",
        ),
    ],
)
def test_bad_http_settings(override: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_settings({**HTTP_ENV, **override})


def test_http_options_for_other_providers() -> None:
    env = {
        **HTTP_ENV,
        "CONTACTS_MCP_PORT": "9000",
        "CONTACTS_MCP_OIDC_UPSTREAM_SCOPES": "openid email",
        "CONTACTS_MCP_OIDC_EXTRA_AUTHORIZE_PARAMS": "access_type=offline&prompt=consent",
        "CONTACTS_MCP_OIDC_VERIFY_ID_TOKEN": "true",
        "CONTACTS_MCP_ALLOWED_REDIRECT_URIS": "https://claude.ai/api/mcp/auth_callback",
        "CONTACTS_MCP_TRUST_FORWARDED_HEADERS": "yes",
        "CONTACTS_MCP_FORWARDED_ALLOW_IPS": "10.0.0.2",
    }
    http = load_settings(env).http
    assert http is not None
    assert http.port == 9000
    assert http.upstream_scopes == "openid email"
    assert http.extra_authorize_params == {"access_type": "offline", "prompt": "consent"}
    assert http.verify_id_token is True
    assert http.allowed_redirect_uris == ("https://claude.ai/api/mcp/auth_callback",)
    assert http.trust_forwarded is True
    assert http.forwarded_allow_ips == "10.0.0.2"
