"""``contacts-mcp auth``: the linking flows (paste and --listen), list, remove, set-read-only."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import respx
from pydantic import SecretStr

from contacts_mcp import auth_cli
from contacts_mcp.accounts import AccountFileStore, AccountRecord
from contacts_mcp.cli import main as cli_main
from contacts_mcp.fields import PEOPLE_API_URL, SCOPES
from contacts_mcp.google.oauth import AUTH_URL, REVOKE_URL, TOKEN_URL

CLIENT_ID = "cli-client-id.apps.example"
CLIENT_SECRET = "cli-client-SECRET-value"
CODE = "4/authorization-code-123"
REFRESH = "1//new-refresh-token-xyz"
ACCESS = "ya29.new-access-token"
OLD_REFRESH = "1//old-refresh-token-abc"
SECRETS = (CLIENT_SECRET, CODE, REFRESH, ACCESS, OLD_REFRESH)
CONNECTIONS = f"{PEOPLE_API_URL}/people/me/connections"

Respond = Callable[[dict[str, str]], str]


def _jwt(claims: dict[str, object]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{payload}.sig"


def id_token(email: str = "me@example.com", **claims: object) -> str:
    return _jwt(
        {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "email": email,
            "email_verified": True,
            **claims,
        }
    )


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    monkeypatch.setenv("CONTACTS_MCP_DATA_DIR", str(path))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    for name in (
        "GOOGLE_CLIENT_ID_FILE",
        "GOOGLE_CLIENT_SECRET_FILE",
        "GOOGLE_CLIENT_SECRETS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def store(data_dir: Path) -> AccountFileStore:
    return AccountFileStore(data_dir)


@pytest.fixture
def google(data_dir: Path) -> Iterator[respx.MockRouter]:
    """Mocked Google: token endpoint, the People API check and revoke; loopback passes."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route(host="127.0.0.1").pass_through()
        router.post(TOKEN_URL, name="token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "expires_in": 3599,
                    "scope": " ".join(auth_cli.LINK_SCOPES),
                    "token_type": "Bearer",
                    "id_token": id_token(),
                },
            )
        )
        router.get(CONNECTIONS, name="people").mock(
            return_value=httpx.Response(200, json={"totalPeople": 0})
        )
        router.post(REVOKE_URL, name="revoke").mock(return_value=httpx.Response(200))
        yield router


def seed(
    store: AccountFileStore,
    alias: str = "work",
    *,
    email: str | None = "old@example.com",
    read_only: bool = False,
    scopes: tuple[str, ...] = SCOPES,
) -> None:
    store.save(
        AccountRecord(
            alias=alias,
            email=email,
            refresh_token=SecretStr(OLD_REFRESH),
            scopes=list(scopes),
            read_only=read_only,
            linked_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )


def assert_no_secrets(*streams: str) -> None:
    for text in streams:
        for secret in SECRETS:
            assert secret not in text


def redirect_to(params: dict[str, str], **overrides: str) -> str:
    """What the browser's address bar shows after consent."""
    query = {"state": params["state"], "code": CODE, "scope": " ".join(SCOPES)}
    query.update(overrides)
    return f"{params['redirect_uri']}?{urlencode({k: v for k, v in query.items() if v})}"


class Run:
    def __init__(self, code: int, out: str, err: str, params: dict[str, str] | None) -> None:
        self.code = code
        self.out = out
        self.err = err
        self.params = params


def run_add(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    respond: Respond | None = None,
) -> Run:
    """Run ``auth add`` with ``input()`` answered by ``respond(auth_url_params)``."""
    seen: dict[str, Any] = {"out": "", "err": ""}

    def fake_input(prompt: object = "") -> str:
        drained = capsys.readouterr()
        seen["out"] += drained.out
        seen["err"] += drained.err
        url = next(
            line for line in seen["out"].splitlines() if line.startswith("https://accounts.")
        )
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        seen["params"] = params
        if respond is None:
            raise EOFError
        return respond(params)

    monkeypatch.setattr("builtins.input", fake_input)
    code = auth_cli.main(["add", *argv])
    captured = capsys.readouterr()
    return Run(code, seen["out"] + captured.out, seen["err"] + captured.err, seen.get("params"))


# --- auth add: paste flow ---------------------------------------------------------------------


def test_add_paste_flow(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)

    assert result.code == 0, result.err
    assert result.params is not None
    p = result.params
    assert AUTH_URL in result.out
    assert p["client_id"] == CLIENT_ID
    assert p["response_type"] == "code"
    assert p["scope"].split() == list(auth_cli.LINK_SCOPES)
    assert p["access_type"] == "offline"
    assert p["prompt"] == "consent"
    assert p["include_granted_scopes"] == "false"
    assert p["code_challenge_method"] == "S256"
    assert p["redirect_uri"].startswith("http://127.0.0.1:")
    assert p["redirect_uri"].endswith("/")

    # the code exchange: PKCE verifier matches the challenge, same redirect_uri, client creds
    form = parse_qs(google["token"].calls.last.request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == [CODE]
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [CLIENT_SECRET]
    assert form["redirect_uri"] == [p["redirect_uri"]]
    digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == p["code_challenge"]
    assert google["people"].calls.last.request.headers["authorization"] == f"Bearer {ACCESS}"

    # the file
    path = store.path("work")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.accounts_dir.stat().st_mode) == 0o700
    saved = store.load("work")
    assert saved.email == "me@example.com"
    assert saved.refresh_token.get_secret_value() == REFRESH
    assert saved.scopes == list(auth_cli.LINK_SCOPES)
    assert saved.read_only is False
    assert saved.linked_at.tzinfo is not None
    assert saved.linked_at.utcoffset() == UTC.utcoffset(None)
    assert "me@example.com" in result.out
    assert "SIGHUP" in result.out
    assert "podman kill --signal HUP" in result.out
    # the instructions mention what to do on the can't-connect page
    assert "address bar" in result.out
    assert_no_secrets(result.out, result.err)


def test_add_accepts_only_the_query_string(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def query_only(params: dict[str, str]) -> str:
        return "?" + urlsplit(redirect_to(params)).query

    assert run_add(capsys, monkeypatch, ["work"], query_only).code == 0
    assert store.load("work").email == "me@example.com"


def test_add_with_fixed_port_and_read_only(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_add(capsys, monkeypatch, ["work", "--port", "8765", "--read-only"], redirect_to)
    assert result.code == 0
    assert result.params is not None
    assert result.params["redirect_uri"] == "http://127.0.0.1:8765/"
    assert store.load("work").read_only is True
    assert "read-only" in result.out


def test_add_refuses_a_state_mismatch(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_add(capsys, monkeypatch, ["work"], lambda p: redirect_to(p, state="forged-state"))
    assert result.code == 1
    assert "state" in result.err
    assert not google["token"].called
    assert not store.path("work").exists()
    assert_no_secrets(result.out, result.err)


def test_add_refuses_a_missing_state(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_add(capsys, monkeypatch, ["work"], lambda p: redirect_to(p, state=""))
    assert result.code == 1
    assert not google["token"].called


def test_add_access_denied(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(p: dict[str, str]) -> str:
        return f"{p['redirect_uri']}?error=access_denied&state={p['state']}"

    result = run_add(capsys, monkeypatch, ["work"], denied)
    assert result.code == 1
    assert "denied" in result.err
    assert not google["token"].called
    assert not store.path("work").exists()


def test_add_url_without_code(
    google: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_add(
        capsys, monkeypatch, ["work"], lambda p: f"{p['redirect_uri']}?state={p['state']}"
    )
    assert result.code == 1
    assert "code" in result.err


def test_add_nothing_pasted_or_eof(
    google: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_add(capsys, monkeypatch, ["work"], lambda _: "   ").code == 1
    assert run_add(capsys, monkeypatch, ["work"], None).code == 1
    assert not google["token"].called


def test_add_refuses_missing_scope(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google["token"].mock(
        return_value=httpx.Response(
            200,
            json={"access_token": ACCESS, "refresh_token": REFRESH, "scope": SCOPES[0]},
        )
    )
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 1
    assert "every permission" in result.err or "all the boxes" in result.err
    assert SCOPES[1] in result.err
    assert not store.path("work").exists()
    assert not google["people"].called
    assert_no_secrets(result.out, result.err)


def test_add_without_refresh_token(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google["token"].mock(
        return_value=httpx.Response(200, json={"access_token": ACCESS, "scope": " ".join(SCOPES)})
    )
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 1
    assert "no refresh token" in result.err
    assert not store.path("work").exists()
    assert_no_secrets(result.out, result.err)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("invalid_grant", "expired or was already used"),
        ("redirect_uri_mismatch", "Desktop app"),
        ("invalid_client", "GOOGLE_CLIENT_ID"),
        ("weird", "weird"),
    ],
)
def test_add_token_endpoint_errors(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    expected: str,
) -> None:
    google["token"].mock(
        return_value=httpx.Response(400, json={"error": error, "error_description": "Bad Request"})
    )
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 1
    assert expected in result.err
    assert not store.path("work").exists()
    assert_no_secrets(result.out, result.err)


def test_add_token_endpoint_unreachable(
    google: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google["token"].mock(side_effect=httpx.ConnectError("boom"))
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 1
    assert "could not reach Google" in result.err


def test_add_people_api_disabled(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google["people"].mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {
                    "message": "disabled",
                    "status": "PERMISSION_DENIED",
                    "details": [{"reason": "SERVICE_DISABLED"}],
                }
            },
        )
    )
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 1
    assert "People API is not enabled" in result.err
    assert not store.path("work").exists()


def test_add_other_403_names_googles_reason(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google["people"].mock(
        return_value=httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
    )
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 1
    assert "PERMISSION_DENIED" in result.err
    assert "not enabled" not in result.err
    assert not store.path("work").exists()


def test_add_takes_the_email_from_the_id_token(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google["token"].mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "scope": " ".join(auth_cli.LINK_SCOPES),
                "id_token": id_token("owner@example.com"),
            },
        )
    )
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 0, result.err
    assert store.load("work").email == "owner@example.com"


@pytest.mark.parametrize(
    "token",
    [
        None,
        "not-a-jwt",
        id_token("someone@example.com", email_verified=False),
        id_token("someone@example.com", aud="another-client"),
        id_token("someone@example.com", iss="https://evil.example"),
    ],
    ids=["missing", "malformed", "unverified", "other-audience", "other-issuer"],
)
def test_add_without_a_usable_id_token_email_saves_the_account_with_a_warning(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    body: dict[str, object] = {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "scope": " ".join(auth_cli.LINK_SCOPES),
    }
    if token is not None:
        body["id_token"] = token
    google["token"].mock(return_value=httpx.Response(200, json=body))
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 0, result.err
    assert "verified e-mail" in result.err
    assert store.load("work").email is None


def test_add_relink_keeps_read_only_and_warns_on_email_change(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(store, read_only=True, email="old@example.com")
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 0
    assert "old@example.com" in result.err
    assert "me@example.com" in result.err
    saved = store.load("work")
    assert saved.read_only is True
    assert saved.email == "me@example.com"
    assert saved.refresh_token.get_secret_value() == REFRESH


def test_add_relink_no_warning_when_email_is_the_same(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(store, email="me@example.com")
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 0
    assert result.err == ""


def test_add_relink_read_write_flag_overrides(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(store, read_only=True)
    assert run_add(capsys, monkeypatch, ["work", "--read-write"], redirect_to).code == 0
    assert store.load("work").read_only is False


def test_add_replaces_an_unreadable_file(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.accounts_dir.mkdir(parents=True)
    store.path("work").write_text("garbage")
    result = run_add(capsys, monkeypatch, ["work"], redirect_to)
    assert result.code == 0
    assert "unusable" in result.err
    assert store.load("work").email == "me@example.com"


# --- usage and configuration errors -----------------------------------------------------------


def test_add_invalid_alias_is_a_usage_error(
    google: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    assert auth_cli.main(["add", "Bad Alias"]) == 2
    assert "invalid account alias" in capsys.readouterr().err


def test_add_without_google_client_is_a_config_error(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    assert auth_cli.main(["add", "work"]) == 2
    assert "GOOGLE_CLIENT_SECRETS_FILE" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["frobnicate"],
        ["add"],
        ["add", "work", "--listen", "70000"],
        ["add", "work", "--port", "0"],
        ["add", "work", "--listen", "8000", "--port", "8001"],
        ["add", "work", "--read-only", "--read-write"],
        ["set-read-only", "work", "maybe"],
        ["remove"],
    ],
)
def test_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert auth_cli.main(argv) == 2
    assert "usage:" in capsys.readouterr().err


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert auth_cli.main(["add", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--listen" in out
    assert "--read-only" in out


def test_cli_dispatches_auth(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli_main(["auth", "list"])
    assert info.value.code == 0
    assert "No accounts linked" in capsys.readouterr().out


# --- auth add --listen ------------------------------------------------------------------------


class Background:
    """``auth add --listen 0`` running in a thread, with its printed output collected."""

    def __init__(self, capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
        self.capsys = capsys
        self.out = ""
        self.err = ""
        self.code: int | None = None
        self.thread = threading.Thread(target=self._run, args=(argv,))
        self.thread.start()

    def _run(self, argv: list[str]) -> None:
        self.code = auth_cli.main(argv)

    def wait_for(self, text: str, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while text not in self.out:
            self._drain()
            assert time.monotonic() < deadline, f"never printed {text!r}: {self.out!r}"
            time.sleep(0.02)
        return self.out

    def _drain(self) -> None:
        drained = self.capsys.readouterr()
        self.out += drained.out
        self.err += drained.err

    def auth_params(self) -> dict[str, str]:
        out = self.wait_for("https://accounts.")
        url = next(line for line in out.splitlines() if line.startswith("https://accounts."))
        return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}

    def finish(self) -> int:
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()
        self._drain()
        assert self.code is not None
        return self.code


def test_add_listen_flow(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    job = Background(capsys, ["add", "work", "--listen", "0"])
    params = job.auth_params()
    redirect = params["redirect_uri"]
    assert redirect.startswith("http://127.0.0.1:")
    port = urlsplit(redirect).port
    job.wait_for("ssh -L")
    assert f"ssh -L {port}:127.0.0.1:{port}" in job.out

    stray = httpx.get(f"{redirect}favicon.ico")
    assert stray.status_code == 404
    forged = httpx.get(redirect, params={"code": CODE, "state": "forged"})
    assert forged.status_code == 400
    assert not google["token"].called

    ok = httpx.get(redirect, params={"code": CODE, "state": params["state"], "scope": "x"})
    assert ok.status_code == 200
    assert "close this tab" in ok.text
    assert job.finish() == 0

    form = parse_qs(google["token"].calls.last.request.content.decode())
    assert form["redirect_uri"] == [redirect]
    saved = store.load("work")
    assert saved.email == "me@example.com"
    assert stat.S_IMODE(store.path("work").stat().st_mode) == 0o600
    assert_no_secrets(job.out, job.err)


def test_add_listen_error_from_google(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    job = Background(capsys, ["add", "work", "--listen", "0"])
    params = job.auth_params()
    response = httpx.get(
        params["redirect_uri"], params={"error": "access_denied", "state": params["state"]}
    )
    assert response.status_code == 400
    assert job.finish() == 1
    assert "denied" in job.err
    assert not store.path("work").exists()


def test_add_listen_times_out(
    google: respx.MockRouter,
    store: AccountFileStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_cli, "LISTEN_TIMEOUT", 0.3)
    assert auth_cli.main(["add", "work", "--listen", "0"]) == 1
    assert "Timed out" in capsys.readouterr().err
    assert not store.path("work").exists()


def test_add_listen_port_in_use(
    google: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert auth_cli.main(["add", "work", "--listen", str(port)]) == 1
    assert "cannot listen" in capsys.readouterr().err


# --- auth list --------------------------------------------------------------------------------


def test_list_empty(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert auth_cli.main(["list"]) == 0
    assert "No accounts linked" in capsys.readouterr().out


def test_list(store: AccountFileStore, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store, "work", email="work@example.com")
    seed(store, "home", email="home@example.com", read_only=True)
    seed(store, "partial", scopes=(SCOPES[0],))
    store.path("work").parent.joinpath("broken.json").write_text(
        f'{{"refresh_token": "{OLD_REFRESH}'
    )

    assert auth_cli.main(["list"]) == 0
    captured = capsys.readouterr()
    lines = {line.split()[0]: line for line in captured.out.splitlines()[1:]}

    assert lines["work"].split()[1:4] == ["work@example.com", "read-write", "complete"]
    assert lines["home"].split()[1:4] == ["home@example.com", "read-only", "complete"]
    assert "INCOMPLETE" in lines["partial"]
    assert "unreadable: accounts/broken.json: invalid JSON" in lines["broken"]
    assert lines["work"].rstrip().endswith("ok")
    assert_no_secrets(captured.out, captured.err)


# --- auth remove ------------------------------------------------------------------------------


def test_remove_revokes_and_deletes(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    assert auth_cli.main(["remove", "work"]) == 0
    assert not store.path("work").exists()
    assert parse_qs(google["revoke"].calls.last.request.content.decode()) == {
        "token": [OLD_REFRESH]
    }
    captured = capsys.readouterr()
    assert "Revoked" in captured.out
    assert "SIGHUP" in captured.out
    assert_no_secrets(captured.out, captured.err)


def test_remove_tolerates_revoke_failure(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    google["revoke"].mock(return_value=httpx.Response(500))
    assert auth_cli.main(["remove", "work"]) == 0
    assert not store.path("work").exists()
    assert "did not revoke" in capsys.readouterr().err


def test_remove_tolerates_unreachable_google(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    google["revoke"].mock(side_effect=httpx.ConnectError("down"))
    assert auth_cli.main(["remove", "work"]) == 0
    assert not store.path("work").exists()
    assert "could not revoke" in capsys.readouterr().err


def test_remove_already_revoked_token(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    google["revoke"].mock(return_value=httpx.Response(400, json={"error": "invalid_token"}))
    assert auth_cli.main(["remove", "work"]) == 0
    captured = capsys.readouterr()
    assert "already revoked" in captured.out
    assert captured.err == ""


def test_remove_no_revoke(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    assert auth_cli.main(["remove", "work", "--no-revoke"]) == 0
    assert not google["revoke"].called
    assert not store.path("work").exists()


def test_remove_unreadable_file_is_still_deleted(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.accounts_dir.mkdir(parents=True)
    store.path("work").write_text("garbage")
    assert auth_cli.main(["remove", "work"]) == 0
    assert not google["revoke"].called
    assert not store.path("work").exists()
    assert "cannot read" in capsys.readouterr().err


def test_remove_unknown_account(
    google: respx.MockRouter, store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert auth_cli.main(["remove", "nope"]) == 1
    assert "no account 'nope'" in capsys.readouterr().err
    assert not google["revoke"].called


# --- auth set-read-only -----------------------------------------------------------------------


def test_set_read_only(store: AccountFileStore, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store)
    assert auth_cli.main(["set-read-only", "work", "on"]) == 0
    assert store.load("work").read_only is True
    out = capsys.readouterr().out
    assert "now read-only" in out
    assert "SIGHUP" in out

    assert auth_cli.main(["set-read-only", "work", "off"]) == 0
    loaded = store.load("work")
    assert loaded.read_only is False
    # everything else is preserved, including the token and the mode
    assert loaded.refresh_token.get_secret_value() == OLD_REFRESH
    assert loaded.email == "old@example.com"
    assert stat.S_IMODE(store.path("work").stat().st_mode) == 0o600
    assert_no_secrets(capsys.readouterr().out)


def test_set_read_only_unchanged(
    store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store, read_only=True)
    before = store.path("work").read_bytes()
    assert auth_cli.main(["set-read-only", "work", "on"]) == 0
    assert "already read-only" in capsys.readouterr().out
    assert store.path("work").read_bytes() == before


def test_set_read_only_unknown_or_broken(
    store: AccountFileStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert auth_cli.main(["set-read-only", "nope", "on"]) == 1
    assert "no such account file" in capsys.readouterr().err

    store.accounts_dir.mkdir(parents=True)
    store.path("work").write_text(json.dumps({"alias": "work"}))
    assert auth_cli.main(["set-read-only", "work", "on"]) == 1
    assert "invalid content" in capsys.readouterr().err
