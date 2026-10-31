"""The settings page (``/settings``): the linked accounts, managed from a browser.

Only in HTTP mode, with ``CONTACTS_MCP_SETTINGS_PAGE``. Every route needs a session from
:mod:`contacts_mcp.web.session`. Every POST must come from this origin (``Origin``, else
``Sec-Fetch-Site: same-origin``) and carry the session's CSRF token; removing or re-linking
an account also needs a recent sign-in. Changes go through the account store, then
``registry.reload()``, so the tools see them at once; each one leaves an audit line.

Linking needs the optional *Web application* Google client: Google redirects back to
:data:`GOOGLE_CALLBACK_PATH`, and the link started by this session is completed there.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from fastmcp import FastMCP
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from contacts_mcp.accounts import AccountFileError, AccountRecord, validate_alias
from contacts_mcp.config import GoogleClientConfig, HttpSettings
from contacts_mcp.google.oauth import AUTH_URL
from contacts_mcp.linking import (
    LinkError,
    LinkErrorKind,
    authorization_url,
    complete_link,
    new_pkce,
    new_state,
    revoke,
)
from contacts_mcp.logs import AuditOutcome, audit
from contacts_mcp.oidc import AllowlistOIDCProxy
from contacts_mcp.registry import Account, AccountRegistry
from contacts_mcp.web.page import escape, layout, message_page, page_response
from contacts_mcp.web.session import (
    CALLBACK_PATH,
    MAX_PENDING,
    PENDING_SECONDS,
    PREFIX,
    SIGNIN_COOKIE,
    BrowserSignIn,
    Expiring,
    Session,
    SignInError,
)

log = logging.getLogger(__name__)

MAX_FORM_BYTES = 4096
Handler = Callable[[Request], Awaitable[Response]]
STATUS_LABELS = {"ok": "OK", "needs_relink": "Needs re-link", "unreadable": "Unreadable"}
GOOGLE_CALLBACK_PATH = f"{PREFIX}/google/callback"
_GOOGLE_AUTH = urlsplit(AUTH_URL)
GOOGLE_ORIGIN = f"{_GOOGLE_AUTH.scheme}://{_GOOGLE_AUTH.netloc}"
#: What to do about a linking failure, on the page.
LINK_HINTS: dict[LinkErrorKind, str] = {
    "invalid_grant": "Link the account again.",
    "redirect_uri_mismatch": "Add {redirect_uri} to the Web client's authorized redirect URIs.",
    "invalid_client": "Check GOOGLE_WEB_CLIENT_ID and GOOGLE_WEB_CLIENT_SECRET.",
}


@dataclass
class PendingLink:
    verifier: str
    alias: str
    #: ``None`` on a re-link: keep the account's current setting.
    read_only: bool | None
    relink: bool
    #: Binds the link to the session that started it.
    session_csrf: str


class ForeignRequestError(Exception):
    """A POST that did not come from this page."""


class SettingsPage:
    def __init__(
        self, http_settings: HttpSettings, registry: AccountRegistry, signin: BrowserSignIn
    ) -> None:
        self.settings = http_settings
        self.registry = registry
        self.signin = signin
        base = urlsplit(http_settings.base_url)
        self.origin = f"{base.scheme}://{base.netloc}".lower()
        self.google_redirect_uri = http_settings.base_url + GOOGLE_CALLBACK_PATH
        self._links: Expiring[PendingLink] = Expiring(PENDING_SECONDS, MAX_PENDING)

    # -- plumbing -------------------------------------------------------------------

    def _page(self, title: str, body: str, *, status_code: int = 200) -> Response:
        return page_response(
            layout(title, body),
            status_code=status_code,
            form_action_origins=self._form_action_origins(),
        )

    def _form_action_origins(self) -> list[str]:
        return [self.signin.provider_origin, GOOGLE_ORIGIN]

    async def _form(self, request: Request, session: Session) -> dict[str, str]:
        """The POSTed form, once it is shown to come from this page. Raises ForeignRequestError."""
        origin = request.headers.get("origin")
        if origin is not None:
            if origin.lower() != self.origin:
                raise ForeignRequestError
        elif request.headers.get("sec-fetch-site") != "same-origin":
            raise ForeignRequestError
        length = request.headers.get("content-length", "")
        if not length.isdigit() or int(length) > MAX_FORM_BYTES:
            raise ForeignRequestError
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            raise ForeignRequestError
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        if not secrets.compare_digest(form.get("csrf", "").encode(), session.csrf.encode()):
            raise ForeignRequestError
        return form

    def _forbidden(self) -> Response:
        return message_page(
            "Refused",
            "This request did not come from the settings page. Reload it and try again.",
            status_code=403,
            link=PREFIX,
        )

    def _back(self, session: Session, notice: str) -> RedirectResponse:
        session.notice = notice
        return RedirectResponse(PREFIX, status_code=303)

    def _audit(
        self,
        tool: str,
        alias: str,
        session: Session,
        outcome: AuditOutcome,
        detail: str | None,
        fields: tuple[str, ...] = (),
    ) -> None:
        audit(
            tool=tool,
            account=alias,
            resource_name=None,
            fields=fields,
            outcome=outcome,
            detail=detail,
            user=session.user,
        )

    def _account(self, alias: str) -> Account | None:
        return next((a for a in self.registry.all() if a.alias == alias), None)

    # -- routes ---------------------------------------------------------------------

    async def index(self, request: Request) -> Response:
        session = self.signin.session(request)
        if session is None:
            return self.signin.start(PREFIX)
        notice, session.notice = session.notice, None
        return self._page("ContactsMCP settings", self._render_index(session, notice))

    async def callback(self, request: Request) -> Response:
        response = RedirectResponse(PREFIX, status_code=303)
        try:
            return_to = await self.signin.finish(request, response)
        except SignInError as e:
            failed = message_page("Not signed in", str(e), status_code=403, link=PREFIX)
            failed.delete_cookie(SIGNIN_COOKIE, path="/", secure=True, httponly=True)
            return failed
        response.headers["location"] = return_to
        return response

    async def sign_out(self, request: Request) -> Response:
        session = self.signin.session(request)
        if session is not None:
            try:
                await self._form(request, session)
            except ForeignRequestError:
                return self._forbidden()
        response = message_page("Signed out", "You are signed out.", link=PREFIX)
        self.signin.sign_out(request, response)
        return response

    async def set_read_only(self, request: Request) -> Response:
        session = self.signin.session(request)
        if session is None:
            return self.signin.start(PREFIX)
        try:
            form = await self._form(request, session)
        except ForeignRequestError:
            return self._forbidden()
        alias = request.path_params["alias"]
        on = form.get("value") == "on"
        store = self.registry.store
        try:
            record = store.load(alias)
            if record.read_only != on:
                store.save(record.model_copy(update={"read_only": on}))
        except (OSError, AccountFileError) as e:
            self._audit(
                "settings.set_read_only", alias, session, "error", _reason(e), ("read_only",)
            )
            return self._back(session, f"Could not change '{alias}': {_reason(e)}")
        self.registry.reload()
        self._audit(
            "settings.set_read_only", alias, session, "ok", "on" if on else "off", ("read_only",)
        )
        return self._back(session, f"'{alias}' is now {'read-only' if on else 'read-write'}.")

    async def confirm_remove(self, request: Request) -> Response:
        session = self.signin.session(request)
        if session is None:
            return self.signin.start(PREFIX)
        account = self._account(request.path_params["alias"])
        if account is None:
            return self._back(session, "That account is not linked.")
        alias = escape(account.alias)
        body = f"""<h1>Remove '{alias}'?</h1>
<p>The account file is deleted and the grant revoked at Google. Revoking cancels this
app's access to {escape(account.email or "that Google account")} for every alias linked to it.
Contacts stay in Google.</p>
<form method="post" action="{PREFIX}/accounts/{alias}/remove">
<input type="hidden" name="csrf" value="{escape(session.csrf)}">
<div class="actions"><button type="submit">Remove</button> <a href="{PREFIX}">Cancel</a></div>
</form>"""
        return self._page("Remove account", body)

    async def remove(self, request: Request) -> Response:
        session = self.signin.session(request)
        if session is None:
            return self.signin.start(PREFIX)
        try:
            await self._form(request, session)
        except ForeignRequestError:
            return self._forbidden()
        alias = request.path_params["alias"]
        if not session.is_fresh(time.time()):
            # Back to the confirmation page once the provider has asked for the login again.
            return self.signin.start(f"{PREFIX}/accounts/{alias}/remove", step_up=True)
        store = self.registry.store
        try:
            record = store.load(alias)
        except AccountFileError:
            record = None
            if not store.path(alias).exists():
                return self._back(session, "That account is not linked.")
        outcome = "not_revoked"
        if record is not None:
            outcome = await revoke(self.registry.http, record.refresh_token.get_secret_value())
        try:
            store.delete(alias)
        except AccountFileError as e:
            self._audit("settings.remove", alias, session, "error", f"revoke: {outcome}; {e}")
            return self._back(session, f"Could not remove '{alias}': {e}")
        self.registry.reload()
        self._audit("settings.remove", alias, session, "ok", f"revoke: {outcome}")
        notice = f"Removed '{alias}'."
        if outcome == "failed":
            notice += (
                " Google did not confirm revoking the grant: check "
                "https://myaccount.google.com/permissions"
            )
        return self._back(session, notice)

    async def link(self, request: Request) -> Response:
        session = self.signin.session(request)
        if session is None:
            return self.signin.start(PREFIX)
        try:
            form = await self._form(request, session)
        except ForeignRequestError:
            return self._forbidden()
        client = self.registry.web_google
        if client is None:
            return self._back(session, "Linking from this page is not configured.")
        alias = form.get("alias", "").strip().lower()
        try:
            validate_alias(alias)
        except ValueError as e:
            return self._back(session, str(e))
        if self._account(alias) is not None:
            return self._back(session, f"'{alias}' is already linked: use its Re-link button.")
        return self._to_google(client, session, alias, form.get("read_only") == "on", relink=False)

    async def relink(self, request: Request) -> Response:
        session = self.signin.session(request)
        if session is None:
            return self.signin.start(PREFIX)
        try:
            await self._form(request, session)
        except ForeignRequestError:
            return self._forbidden()
        client = self.registry.web_google
        if client is None:
            return self._back(session, "Linking from this page is not configured.")
        account = self._account(request.path_params["alias"])
        if account is None:
            return self._back(session, "That account is not linked.")
        if not session.is_fresh(time.time()):
            return self.signin.start(
                PREFIX,
                step_up=True,
                notice=f"You signed in again: press Re-link for '{account.alias}' once more.",
            )
        return self._to_google(
            client, session, account.alias, None, relink=True, login_hint=account.email
        )

    def _to_google(
        self,
        client: GoogleClientConfig,
        session: Session,
        alias: str,
        read_only: bool | None,
        *,
        relink: bool,
        login_hint: str | None = None,
    ) -> Response:
        pkce = new_pkce()
        state = new_state()
        self._links.put(state, PendingLink(pkce.verifier, alias, read_only, relink, session.csrf))
        url = authorization_url(
            client,
            self.google_redirect_uri,
            state,
            pkce.challenge,
            login_hint=login_hint,
        )
        return RedirectResponse(url, status_code=303)

    async def google_callback(self, request: Request) -> Response:
        session = self.signin.session(request)
        if session is None:
            return message_page(
                "Not linked",
                "Your session ended before Google answered. Sign in and link the account again.",
                status_code=403,
                link=PREFIX,
            )
        params = request.query_params
        state = params.get("state", "")
        pending = self._links.pop(state) if state else None
        if pending is None or not secrets.compare_digest(
            pending.session_csrf.encode(), session.csrf.encode()
        ):
            return self._back(
                session, "That link expired or was not started here. Link the account again."
            )
        alias = pending.alias
        tool = "settings.relink" if pending.relink else "settings.link"
        error = params.get("error")
        if error:
            self._audit(tool, alias, session, "refused", f"google: {error[:100]}")
            if error == "access_denied":
                return self._back(session, f"Linking '{alias}' was cancelled at Google.")
            return self._back(session, f"Google reported an error: {error[:100]}")
        web_google = self.registry.web_google
        if web_google is None:
            return self._back(session, "Linking from this page is not configured any more.")
        code = params.get("code")
        if not code:
            return self._back(session, "Google sent no authorization code. Link again.")
        store = self.registry.store
        existing: AccountRecord | None = None
        if pending.relink:
            try:
                existing = store.load(alias)
            except AccountFileError:
                self._audit(tool, alias, session, "refused", "account file unreadable or gone")
                return self._back(
                    session,
                    f"'{alias}' was removed or its file cannot be read: remove it and link it "
                    "again.",
                )
        elif store.path(alias).exists():
            self._audit(tool, alias, session, "refused", "alias taken")
            return self._back(session, f"'{alias}' was linked meanwhile: use its Re-link button.")

        try:
            grant = await complete_link(
                self.registry.http, web_google, code, self.google_redirect_uri, pending.verifier
            )
        except LinkError as e:
            hint = LINK_HINTS.get(e.kind, "") if e.kind else ""
            hint = hint.format(redirect_uri=self.google_redirect_uri)
            self._audit(tool, alias, session, "error", e.kind or "link failed")
            return self._back(session, f"Could not link '{alias}': {e} {hint}".strip())

        read_only = existing.read_only if existing is not None else bool(pending.read_only)
        record = AccountRecord(
            alias=alias,
            email=grant.email,
            refresh_token=SecretStr(grant.refresh_token),
            scopes=grant.scopes,
            read_only=read_only,
            linked_at=datetime.now(UTC),
            client="web",
        )
        try:
            store.save(record)
        except (OSError, AccountFileError) as e:
            self._audit(tool, alias, session, "error", f"save: {_reason(e)}")
            return self._back(session, f"Could not save '{alias}': {_reason(e)}")
        self.registry.reload()
        changed = existing is not None and existing.email != grant.email
        self._audit(tool, alias, session, "ok", "google account changed" if changed else None)
        notice = (
            f"Linked {grant.email or 'the account'} as '{alias}'"
            + (" (read-only)" if read_only else "")
            + "."
        )
        if changed and existing is not None and existing.email:
            notice += f" It was linked to {existing.email} before."
        return self._back(session, notice)

    # -- rendering ------------------------------------------------------------------

    def _render_index(self, session: Session, notice: str | None) -> str:
        csrf = escape(session.csrf)
        rows = "".join(self._render_row(a, csrf) for a in self.registry.all())
        accounts = (
            f"""<table>
<thead><tr><th>Alias</th><th>Google account</th><th>Status</th><th>Access</th>
<th>Linked</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table>"""
            if rows
            else "<p>No account is linked.</p>"
        )
        return f"""<nav><h1>ContactsMCP</h1><span class="hint">{escape(session.user)}</span>
<form method="post" action="{PREFIX}/sign-out"><input type="hidden" name="csrf" value="{csrf}">
<button type="submit">Sign out</button></form></nav>
{f'<p class="notice">{escape(notice)}</p>' if notice else ""}
<h2>Accounts</h2>
{accounts}
{self._render_link_section(csrf)}"""

    def _render_link_section(self, csrf: str) -> str:
        if self.registry.web_google is None:
            return """<h2>Link an account</h2>
<p class="hint">Linking from this page needs a Google OAuth client of type
<em>Web application</em> (<code>GOOGLE_WEB_CLIENT_ID</code> and
<code>GOOGLE_WEB_CLIENT_SECRET</code>). Until then, link accounts on the server with
<code>contacts-mcp auth add &lt;alias&gt;</code>; then send the server a <code>SIGHUP</code>.</p>"""
        return f"""<h2>Link an account</h2>
<form method="post" action="{PREFIX}/link">
<input type="hidden" name="csrf" value="{csrf}">
<label for="alias">Alias</label>
<input type="text" id="alias" name="alias" required maxlength="32"
 pattern="[a-z0-9][a-z0-9\\-]{{0,31}}" autocomplete="off">
<p class="hint">1-32 lowercase letters, digits and '-': the name tools use for the account.</p>
<label class="inline"><input type="checkbox" name="read_only" value="on"> Read-only</label>
<div class="actions"><button type="submit">Link with Google</button></div>
</form>"""

    def _render_row(self, account: Account, csrf: str) -> str:
        alias = escape(account.alias)
        status = STATUS_LABELS.get(account.status, account.status)
        if account.status == "unreadable" and account.error:
            status += f'<br><span class="hint">{escape(account.error)}</span>'
        linked = ""
        if account.record is not None:
            linked = escape(_date(account.record.linked_at))
        toggle = ""
        if account.record is not None:
            value = "off" if account.read_only else "on"
            label = "Allow writes" if account.read_only else "Make read-only"
            toggle = f"""<form method="post" action="{PREFIX}/accounts/{alias}/read-only">
<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="value" value="{value}">
<button type="submit">{label}</button></form>"""
        relink = ""
        if self.registry.web_google is not None:
            relink = f"""<form method="post" action="{PREFIX}/accounts/{alias}/relink">
<input type="hidden" name="csrf" value="{csrf}"><button type="submit">Re-link</button></form>"""
        remove = f'<a href="{PREFIX}/accounts/{alias}/remove">Remove</a>'
        return f"""<tr><td>{alias}</td><td>{escape(account.email or "-")}</td><td>{status}</td>
<td>{"Read-only" if account.read_only else "Read-write"}</td><td>{linked}</td>
<td>{toggle} {relink} {remove}</td></tr>"""


def _reason(e: OSError | AccountFileError) -> str:
    return (e.strerror or str(e)) if isinstance(e, OSError) else str(e)


def _date(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _valid_alias(request: Request) -> bool:
    try:
        validate_alias(request.path_params.get("alias", ""))
    except ValueError:
        return False
    return True


def register_settings_page(
    mcp: FastMCP,
    http_settings: HttpSettings,
    registry: AccountRegistry,
    auth: AllowlistOIDCProxy,
) -> SettingsPage:
    """Add the ``/settings`` routes to ``mcp``. They are outside the MCP bearer check."""
    signin = BrowserSignIn(http_settings, auth, registry.http)
    page = SettingsPage(http_settings, registry, signin)

    def by_alias(handler: Handler) -> Handler:
        async def checked(request: Request) -> Response:
            if not _valid_alias(request):
                return message_page("Not found", "No such account.", status_code=404, link=PREFIX)
            return await handler(request)

        return checked

    mcp.custom_route(PREFIX, methods=["GET"])(page.index)
    mcp.custom_route(CALLBACK_PATH, methods=["GET"])(page.callback)
    mcp.custom_route(f"{PREFIX}/sign-out", methods=["POST"])(page.sign_out)
    mcp.custom_route(f"{PREFIX}/accounts/{{alias}}/read-only", methods=["POST"])(
        by_alias(page.set_read_only)
    )
    mcp.custom_route(f"{PREFIX}/accounts/{{alias}}/remove", methods=["GET"])(
        by_alias(page.confirm_remove)
    )
    mcp.custom_route(f"{PREFIX}/accounts/{{alias}}/remove", methods=["POST"])(by_alias(page.remove))
    mcp.custom_route(f"{PREFIX}/link", methods=["POST"])(page.link)
    mcp.custom_route(f"{PREFIX}/accounts/{{alias}}/relink", methods=["POST"])(by_alias(page.relink))
    mcp.custom_route(GOOGLE_CALLBACK_PATH, methods=["GET"])(page.google_callback)
    return page
