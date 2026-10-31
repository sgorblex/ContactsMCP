"""The settings page (``/settings``): the linked accounts, managed from a browser.

Only in HTTP mode, with ``CONTACTS_MCP_SETTINGS_PAGE``. Every route needs a session from
:mod:`contacts_mcp.web.session`. Every POST must come from this origin (``Origin``, else
``Sec-Fetch-Site: same-origin``) and carry the session's CSRF token; removing an account
also needs a recent sign-in. Changes go through the account store, then
``registry.reload()``, so the tools see them at once; each one leaves an audit line.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from contacts_mcp.accounts import AccountFileError, validate_alias
from contacts_mcp.config import HttpSettings
from contacts_mcp.linking import revoke
from contacts_mcp.logs import AuditOutcome, audit
from contacts_mcp.oidc import AllowlistOIDCProxy
from contacts_mcp.registry import Account, AccountRegistry
from contacts_mcp.web.page import escape, layout, message_page, page_response
from contacts_mcp.web.session import (
    CALLBACK_PATH,
    PREFIX,
    SIGNIN_COOKIE,
    BrowserSignIn,
    Session,
    SignInError,
)

log = logging.getLogger(__name__)

MAX_FORM_BYTES = 4096
Handler = Callable[[Request], Awaitable[Response]]
STATUS_LABELS = {"ok": "OK", "needs_relink": "Needs re-link", "unreadable": "Unreadable"}


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

    # -- plumbing -------------------------------------------------------------------

    def _page(self, title: str, body: str, *, status_code: int = 200) -> Response:
        return page_response(
            layout(title, body),
            status_code=status_code,
            form_action_origins=self._form_action_origins(),
        )

    def _form_action_origins(self) -> list[str]:
        return [self.signin.provider_origin]

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
        detail: str,
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
        return """<h2>Link an account</h2>
<p class="hint">Link accounts on the server with <code>contacts-mcp auth add &lt;alias&gt;</code>;
then send the server a <code>SIGHUP</code> (or restart it).</p>"""

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
        remove = f'<a href="{PREFIX}/accounts/{alias}/remove">Remove</a>'
        return f"""<tr><td>{alias}</td><td>{escape(account.email or "-")}</td><td>{status}</td>
<td>{"Read-only" if account.read_only else "Read-write"}</td><td>{linked}</td>
<td>{toggle} {remove}</td></tr>"""


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
    return page
