"""``contacts-mcp auth …``: link, list, remove accounts and toggle read-only.

An interactive command, run against the server's data directory (locally:
``mise exec -- uv run contacts-mcp auth add <alias>``). It is not the MCP stdio server, so
it may print to stdout. It never prints a token, the
client secret or an authorization code.

Linking uses Google's loopback flow for installed apps (Desktop client). By default nothing
listens: the browser is sent to ``http://127.0.0.1:<port>/?code=…``, fails to load it, and
the user pastes the address-bar URL back. ``--listen PORT`` serves that redirect instead.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import SecretStr

from contacts_mcp.accounts import (
    AccountFileError,
    AccountFileStore,
    AccountRecord,
    validate_alias,
)
from contacts_mcp.config import ConfigError, GoogleClientConfig, load_data_dir, load_google_client
from contacts_mcp.fields import PEOPLE_API_URL, SCOPES
from contacts_mcp.google.oauth import AUTH_URL, REVOKE_URL, TOKEN_URL

#: Asked when linking: the contacts scopes, plus `openid email` so the account's own
#: address comes back in the ID token (`contacts` does not allow reading it from
#: people/me, which answers 403).
LINK_SCOPES = (*SCOPES, "openid", "email")
SIGHUP_HINT = (
    "A running server picks this up on SIGHUP (or restart): "
    "`podman kill --signal HUP <container>`, `docker kill --signal HUP <container>` "
    "or `kill -HUP <pid>`."
)
#: The ``iss`` of Google's ID tokens.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

#: Seconds ``--listen`` waits for the browser before giving up.
LISTEN_TIMEOUT = 300.0
HTTP_TIMEOUT = 30.0


class AuthError(Exception):
    """An operation failed; the message is shown to the user as is (and holds no secret)."""


# --- output -----------------------------------------------------------------------------


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _say(message: str = "") -> None:
    print(message)


# --- argument parsing ---------------------------------------------------------------------


def _port(value: str, *, minimum: int) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port number") from None
    if not minimum <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between {minimum} and 65535")
    return port


def _listen_port(value: str) -> int:
    return _port(value, minimum=0)


def _redirect_port(value: str) -> int:
    return _port(value, minimum=1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contacts-mcp auth",
        description="Link Google accounts to ContactsMCP and manage them. Account files live "
        "in $CONTACTS_MCP_DATA_DIR/accounts; the Google OAuth client comes from "
        "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (or GOOGLE_CLIENT_SECRETS_FILE).",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    add = sub.add_parser(
        "add",
        help="link (or re-link) a Google account under an alias",
        description="Link a Google account. By default prints a URL to open in any browser; "
        "after you consent the browser shows a 'can't connect' page at 127.0.0.1: copy the "
        "whole address-bar URL and paste it here. With --listen the redirect is served "
        "for real (locally, or through `ssh -L`).",
    )
    add.add_argument("alias", help="account alias: 1-32 lowercase letters, digits, '-'")
    where = add.add_mutually_exclusive_group()
    where.add_argument(
        "--listen",
        type=_listen_port,
        metavar="PORT",
        help="serve the loopback redirect on 127.0.0.1:PORT (0 picks a free port)",
    )
    where.add_argument(
        "--port",
        type=_redirect_port,
        metavar="PORT",
        help="port in the redirect URL of the paste flow (default: a random high port; "
        "nothing listens on it)",
    )
    mode = add.add_mutually_exclusive_group()
    mode.add_argument(
        "--read-only",
        dest="read_only",
        action="store_const",
        const=True,
        default=None,
        help="refuse write tools on this account",
    )
    mode.add_argument(
        "--read-write",
        dest="read_only",
        action="store_const",
        const=False,
        help="allow write tools (the default; on re-link, keeps the current setting "
        "unless one of the two is given)",
    )

    sub.add_parser("list", help="show linked accounts (never tokens)")

    remove = sub.add_parser(
        "remove",
        help="unlink an account: revoke its grant at Google (best effort), delete its file",
    )
    remove.add_argument("alias")
    remove.add_argument(
        "--no-revoke",
        action="store_true",
        help="only delete the file (revoking cancels the grant for this Google account and "
        "client, which also affects another alias linked to the same account)",
    )

    ro = sub.add_parser("set-read-only", help="mark an account read-only, or writable again")
    ro.add_argument("alias")
    ro.add_argument("value", choices=("on", "off"))
    return parser


def main(argv: list[str]) -> int:
    """Entry point of ``contacts-mcp auth``; returns the exit code (0 ok, 1 failed, 2 usage)."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_OK

    if args.command != "list":
        try:
            alias = validate_alias(args.alias)
        except ValueError as e:
            _err(str(e))
            return EXIT_USAGE

    try:
        if args.command == "list":
            return _cmd_list()
        if args.command == "add":
            return _cmd_add(alias, args)
        if args.command == "remove":
            return _cmd_remove(alias, revoke=not args.no_revoke)
        return _cmd_set_read_only(alias, on=args.value == "on")
    except ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    except AuthError as e:
        _err(str(e))
        return EXIT_FAILED
    except KeyboardInterrupt:
        print(file=sys.stderr)
        _err("interrupted")
        return EXIT_FAILED


# --- auth add -------------------------------------------------------------------------------


def _cmd_add(alias: str, args: argparse.Namespace) -> int:
    client = load_google_client()
    store = AccountFileStore(load_data_dir())
    existing = _load_existing(store, alias)

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(32)

    if args.listen is not None:
        code, redirect_uri = _listen_flow(client, state, challenge, args.listen)
    else:
        port = args.port or 49152 + secrets.randbelow(65536 - 49152)
        redirect_uri = f"http://127.0.0.1:{port}/"
        code = _paste_flow(_authorization_url(client, redirect_uri, state, challenge), state)

    tokens = _exchange_code(client, code, redirect_uri, verifier)
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthError(
            "Google returned no refresh token. Remove this app's access at "
            "https://myaccount.google.com/permissions and run the command again."
        )
    scope_field = tokens.get("scope")
    granted = scope_field.split() if isinstance(scope_field, str) else []
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise AuthError(
            "Not every permission was granted. Run the command again and tick all the "
            "boxes on Google's consent screen (missing: " + ", ".join(missing) + ")."
        )
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthError("Google returned no access token.")

    _check_people_api(access_token)
    email = _email_from_id_token(tokens.get("id_token"), client)
    if email is None:
        _warn("Google did not return the account's verified e-mail address; saving without it")

    if existing is not None and existing.email and email and existing.email != email:
        _warn(f"'{alias}' was linked to {existing.email}; it is now {email}")
    read_only = (
        args.read_only
        if args.read_only is not None
        else (existing.read_only if existing is not None else False)
    )
    record = AccountRecord(
        alias=alias,
        email=email,
        refresh_token=SecretStr(refresh_token),
        scopes=granted,
        read_only=read_only,
        linked_at=datetime.now(UTC),
    )
    try:
        path = store.save(record)
    except (OSError, AccountFileError) as e:
        raise AuthError(f"cannot write the account file: {_reason(e)}") from None

    _say()
    _say(
        f"Linked {email or 'the account'} as '{alias}'"
        + (" (read-only)" if read_only else "")
        + f". Saved to {path}."
    )
    _say(SIGHUP_HINT)
    return EXIT_OK


def _load_existing(store: AccountFileStore, alias: str) -> AccountRecord | None:
    try:
        return store.load(alias)
    except AccountFileError as e:
        if store.path(alias).exists():
            _warn(f"the current file of '{alias}' is unusable and will be replaced ({e})")
        return None


def _authorization_url(
    client: GoogleClientConfig, redirect_uri: str, state: str, challenge: str
) -> str:
    query = urlencode(
        {
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
    )
    return f"{AUTH_URL}?{query}"


def _state_matches(got: str, expected: str) -> bool:
    return secrets.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


def _code_from_params(params: dict[str, list[str]], state: str) -> str:
    """Validate a redirect's query parameters and return the authorization code."""
    if not _state_matches(params.get("state", [""])[0], state):
        raise AuthError(
            "The 'state' value does not match this session: the URL is from another "
            "attempt (or was altered). Run the command again and paste the new URL."
        )
    error = params.get("error", [""])[0]
    if error:
        if error == "access_denied":
            raise AuthError("Access was denied on Google's consent screen.")
        raise AuthError(f"Google reported an error: {error[:100]}")
    code = params.get("code", [""])[0]
    if not code:
        raise AuthError("The URL has no 'code' parameter; paste the whole address-bar URL.")
    return code


def _parse_pasted(text: str) -> dict[str, list[str]]:
    """Accept the full redirect URL, or just its query string."""
    text = text.strip().strip("\"'<>").strip()
    if not text:
        raise AuthError("Nothing was pasted.")
    if "://" in text or text.startswith("/"):
        query = urlsplit(text).query
    else:
        query = text.partition("?")[2] if "?" in text else text
    return parse_qs(query)


def _paste_flow(url: str, state: str) -> str:
    _say("Open this URL in a browser, on any machine, and sign in with the Google account")
    _say("to link:")
    _say()
    _say(url)
    _say()
    _say("Tick every permission on the consent screen. Google may warn that the app is")
    _say("unverified: that is expected for your own client (Advanced > continue).")
    _say("Afterwards the browser shows a 'can't connect' page at 127.0.0.1: that is normal.")
    _say("Copy the whole URL from the address bar and paste it below.")
    _say()
    try:
        pasted = input("URL: ")
    except EOFError:
        raise AuthError("No URL was provided (end of input).") from None
    return _code_from_params(_parse_pasted(pasted), state)


# --- --listen ---------------------------------------------------------------------------------

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>ContactsMCP</title>"
    "<body style='font-family:sans-serif;margin:3em'><h2>{title}</h2><p>{text}</p></body>"
)


def _listen_flow(
    client: GoogleClientConfig, state: str, challenge: str, port: int
) -> tuple[str, str]:
    """Serve the loopback redirect once. Returns the code and the redirect URI used."""
    received: dict[str, list[str]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        # An idle connection (a browser's speculative preconnect) must not block the
        # single-threaded server past LISTEN_TIMEOUT.
        timeout = 10

        def do_GET(self) -> None:
            params = parse_qs(urlsplit(self.path).query)
            if "code" not in params and "error" not in params:
                self._reply(404, "Not found", "Nothing here.")
            elif not _state_matches(params.get("state", [""])[0], state):
                self._reply(400, "Refused", "Unexpected request; close this tab and try again.")
            elif "error" in params:
                received.update(params)
                self._reply(400, "Not linked", "Google reported an error. See the terminal.")
            else:
                received.update(params)
                self._reply(200, "Authorized", "You can close this tab and return to the terminal.")

        def _reply(self, status: int, title: str, text: str) -> None:
            body = _PAGE.format(title=title, text=text).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the access log: request lines carry the authorization code."""

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise AuthError(f"cannot listen on 127.0.0.1:{port}: {e.strerror}") from None
    bound = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{bound}/"
    server.timeout = 0.5
    try:
        _say("Open this URL in a browser and sign in with the Google account to link:")
        _say()
        _say(_authorization_url(client, redirect_uri, state, challenge))
        _say()
        _say(
            f"Listening on {redirect_uri} for Google's redirect (waits up to "
            f"{int(LISTEN_TIMEOUT)} s). The browser must reach this machine's port {bound}:"
        )
        _say(f"from another machine, run `ssh -L {bound}:127.0.0.1:{bound} <server>` first.")
        _say("Tick every permission on the consent screen.")
        deadline = time.monotonic() + LISTEN_TIMEOUT
        while not received and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not received:
        raise AuthError("Timed out waiting for Google's redirect.")
    return _code_from_params(received, state), redirect_uri


# --- Google calls -------------------------------------------------------------------------------


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _exchange_code(
    client: GoogleClientConfig, code: str, redirect_uri: str, verifier: str
) -> dict[str, Any]:
    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client.client_id,
                "client_secret": client.client_secret.get_secret_value(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise AuthError(f"could not reach Google ({type(e).__name__})") from None
    body = _json(response)
    if response.status_code == 200:
        return body
    error = body.get("error")
    error = error if isinstance(error, str) else ""
    if error == "invalid_grant":
        raise AuthError(
            "Google rejected the authorization code: it expired or was already used. "
            "Run the command again and paste the fresh URL right after consenting."
        )
    if error == "redirect_uri_mismatch":
        raise AuthError(
            "Google rejected the redirect URI (redirect_uri_mismatch): the OAuth client must "
            "be of type 'Desktop app'."
        )
    if error in ("invalid_client", "unauthorized_client"):
        raise AuthError(
            f"Google rejected the OAuth client ({error}): check GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET."
        )
    raise AuthError(
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
    if claims.get("iss") not in GOOGLE_ISSUERS or claims.get("aud") != client.client_id:
        return None
    email = claims.get("email")
    return email if isinstance(email, str) and email else None


def _check_people_api(access_token: str) -> None:
    """Fail now, not at the first tool call, when the People API can't be used."""
    try:
        response = httpx.get(
            f"{PEOPLE_API_URL}/people/me/connections",
            params={"pageSize": 1, "personFields": "names"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise AuthError(f"could not reach the People API ({type(e).__name__})") from None
    if response.status_code == 403:
        reason = _google_reason(response)
        if reason == "SERVICE_DISABLED":
            raise AuthError(
                "the People API is not enabled in the Google Cloud project of the OAuth "
                "client: enable it (APIs & Services → Library → People API) and try again."
            )
        raise AuthError(
            "Google refused access to the contacts (403"
            + (f", {reason}" if reason else "")
            + "). For a Workspace account, an admin may have to allow this OAuth client."
        )
    if response.status_code != 200:
        raise AuthError(f"the People API check failed (HTTP {response.status_code})")


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


# --- auth list / remove / set-read-only -----------------------------------------------------------


def _scan(store: AccountFileStore) -> dict[str, AccountRecord | AccountFileError]:
    try:
        return store.scan()
    except OSError as e:
        raise AuthError(f"cannot read {store.accounts_dir}: {_reason(e)}") from None


def _reason(e: OSError | AccountFileError) -> str:
    return e.strerror or str(e) if isinstance(e, OSError) else str(e)


def _cmd_list() -> int:
    store = AccountFileStore(load_data_dir())
    scanned = _scan(store)
    if not scanned:
        _say(f"No accounts linked ({store.accounts_dir}). Link one with `contacts-mcp auth add`.")
        return EXIT_OK
    rows = [("ALIAS", "EMAIL", "ACCESS", "SCOPES", "FILE")]
    for alias, result in scanned.items():
        if isinstance(result, AccountFileError):
            rows.append((alias, "-", "-", "-", f"unreadable: {result}"))
            continue
        complete = all(s in result.scopes for s in SCOPES)
        rows.append(
            (
                alias,
                result.email or "-",
                "read-only" if result.read_only else "read-write",
                "complete" if complete else "INCOMPLETE (re-link)",
                "ok",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for row in rows:
        _say(
            "  ".join(cell.ljust(w) for cell, w in zip(row[:4], widths, strict=True))
            + "  "
            + row[4]
        )
    return EXIT_OK


def _cmd_remove(alias: str, *, revoke: bool) -> int:
    store = AccountFileStore(load_data_dir())
    record: AccountRecord | None = None
    try:
        record = store.load(alias)
    except AccountFileError as e:
        if not store.path(alias).exists():
            raise AuthError(f"no account '{alias}' is linked") from None
        _warn(f"cannot read the file of '{alias}' ({e}); it cannot be revoked at Google")

    if revoke and record is not None:
        try:
            response = httpx.post(
                REVOKE_URL,
                data={"token": record.refresh_token.get_secret_value()},
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as e:
            _warn(f"could not revoke the grant at Google ({type(e).__name__}); deleting anyway")
        else:
            if response.status_code == 200:
                _say("Revoked the grant at Google.")
            elif _json(response).get("error") == "invalid_token":
                _say("Google says the token was already revoked or expired.")
            else:
                _warn(
                    f"Google did not revoke the grant (HTTP {response.status_code}); deleting "
                    "the file anyway. Check https://myaccount.google.com/permissions"
                )
    try:
        store.delete(alias)
    except AccountFileError as e:
        raise AuthError(str(e)) from None
    _say(f"Removed '{alias}'.")
    _say(SIGHUP_HINT)
    return EXIT_OK


def _cmd_set_read_only(alias: str, *, on: bool) -> int:
    store = AccountFileStore(load_data_dir())
    try:
        record = store.load(alias)
    except AccountFileError as e:
        raise AuthError(str(e)) from None
    if record.read_only == on:
        _say(f"'{alias}' is already {'read-only' if on else 'read-write'}.")
        return EXIT_OK
    try:
        store.save(record.model_copy(update={"read_only": on}))
    except (OSError, AccountFileError) as e:
        raise AuthError(f"cannot write the account file: {_reason(e)}") from None
    _say(f"'{alias}' is now {'read-only' if on else 'read-write'}.")
    _say(SIGHUP_HINT)
    return EXIT_OK
