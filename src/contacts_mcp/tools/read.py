"""Read tools: ``accounts``, ``search_contacts``, ``list_contacts``, ``get_contact``."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from contacts_mcp.errors import ContactsError, InvalidArgumentError
from contacts_mcp.fields import LISTING_FIELDS, READ_FIELDS, SUMMARY_FIELDS
from contacts_mcp.models import (
    AccountArg,
    AccountFailure,
    AccountsResult,
    Contact,
    ContactList,
    ContactSummary,
    PersonResourceName,
    SearchResult,
)
from contacts_mcp.normalize import person_to_contact, person_to_summary
from contacts_mcp.registry import Account, AccountRegistry
from contacts_mcp.tools._common import READ, SEARCH_LAG_NOTE, UNTRUSTED_NOTE

log = logging.getLogger(__name__)

#: ``list_contacts(sort=…)`` → the People API's ``sortOrder``.
SORT_ORDERS = {
    "last_modified_descending": "LAST_MODIFIED_DESCENDING",
    "last_modified_ascending": "LAST_MODIFIED_ASCENDING",
    "first_name": "FIRST_NAME_ASCENDING",
    "last_name": "LAST_NAME_ASCENDING",
}

#: ``accounts`` only reads local files: it never talks to Google.
READ_LOCAL = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

ALL_ACCOUNTS = "*"
SEARCH_MAX = 30


async def _search_one(account: Account, query: str, limit: int) -> list[ContactSummary]:
    """Search one account (warming it up first)."""
    api = account.people  # raises for an unreadable or needs-relink account
    await account.ensure_warm("contacts")
    body = await api.search_contacts(query, page_size=limit, read_mask=SUMMARY_FIELDS)
    groups = await account.group_names(best_effort=True)
    out: list[ContactSummary] = []
    for result in body.get("results", []):
        person = result.get("person") if isinstance(result, dict) else None
        if isinstance(person, dict):
            out.append(person_to_summary(person, group_names=groups, account=account.alias))
    return out


def register(mcp: FastMCP, registry: AccountRegistry) -> None:
    """Register the read tools on ``mcp``."""

    @mcp.tool(
        description=(
            "List the linked Google accounts: alias, e-mail, read_only flag and status "
            "(ok, needs_relink, unreadable, with what to do about it). Call this first. "
            "`default_account` is set when exactly one account is linked, and is then used "
            "when `account` is omitted."
        ),
        annotations=READ_LOCAL,
    )
    async def accounts() -> AccountsResult:
        return AccountsResult(
            accounts=[a.info() for a in registry.all()],
            default_account=registry.default_alias(),
        )

    @mcp.tool(
        description=(
            "Search contacts by name, e-mail, phone number or organization (prefix match on "
            "words). Returns compact summaries (resource name, display name, primary e-mail "
            "and phone, organization, group names); use `get_contact` for everything else. "
            'Pass `account` "*" to search every linked account at once: accounts that could '
            "not be searched are listed in `failures` instead of failing the call. Each "
            "result carries its `account` alias. "
            f"{SEARCH_LAG_NOTE} {UNTRUSTED_NOTE}"
        ),
        annotations=READ,
    )
    async def search_contacts(
        query: Annotated[str, Field(min_length=1, description="Text to search for.")],
        account: Annotated[
            str | None,
            Field(
                description=(
                    'Account alias, or "*" for every account; omit only if just one is linked.'
                )
            ),
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=SEARCH_MAX, description="Maximum results per account.")
        ] = 10,
    ) -> SearchResult:
        query = query.strip()
        if not query:
            raise InvalidArgumentError("`query` must not be blank.")
        if account != ALL_ACCOUNTS:
            single = registry.resolve(account)
            return SearchResult(contacts=await _search_one(single, query, limit))

        targets = registry.all()
        if not targets:
            registry.resolve(None)  # raises "No Google account is linked…"

        async def run(target: Account) -> list[ContactSummary] | AccountFailure:
            try:
                return await _search_one(target, query, limit)
            except ContactsError as e:
                return AccountFailure(account=target.alias, error=str(e))
            except Exception:
                log.exception("search failed", extra={"account": target.alias})
                return AccountFailure(
                    account=target.alias, error="Unexpected error while searching this account."
                )

        contacts: list[ContactSummary] = []
        failures: list[AccountFailure] = []
        for outcome in await asyncio.gather(*(run(t) for t in targets)):
            if isinstance(outcome, AccountFailure):
                failures.append(outcome)
            else:
                contacts.extend(outcome)
        return SearchResult(contacts=contacts, failures=failures)

    @mcp.tool(
        description=(
            "List contacts page by page as compact summaries, which here also have "
            "`updated_at`, `has_notes` and `field_count`. Pass the returned "
            "`next_page_token` as `page_token` to get the next page (absent on the last "
            "page), with the same `sort`. `total_items` is Google's count of the whole list. "
            f"{UNTRUSTED_NOTE}"
        ),
        annotations=READ,
    )
    async def list_contacts(
        account: AccountArg = None,
        page_size: Annotated[int, Field(ge=1, le=1000, description="Contacts per page.")] = 50,
        page_token: Annotated[
            str | None, Field(description="`next_page_token` of the previous page.")
        ] = None,
        sort: Annotated[
            Literal[
                "last_modified_descending", "last_modified_ascending", "first_name", "last_name"
            ]
            | None,
            Field(
                description=(
                    "Sort order. Google's default (oldest change first) when omitted. The "
                    "order can lag recent writes by a few minutes."
                )
            ),
        ] = None,
    ) -> ContactList:
        acct = registry.resolve(account)
        body = await acct.people.list_connections(
            page_size=page_size,
            page_token=page_token,
            sort_order=SORT_ORDERS[sort] if sort else None,
            person_fields=LISTING_FIELDS,
        )
        groups = await acct.group_names(best_effort=True)
        total = body.get("totalItems")
        token = body.get("nextPageToken")
        return ContactList(
            contacts=[
                person_to_summary(p, group_names=groups, account=acct.alias, details=True)
                for p in body.get("connections", [])
                if isinstance(p, dict)
            ],
            next_page_token=token if isinstance(token, str) and token else None,
            total_items=total if isinstance(total, int) else None,
        )

    @mcp.tool(
        description=(
            "Get one contact in full: every supported field (names, e-mails, phones, "
            "organizations, addresses, birthday, notes, URLs, relations, events, custom "
            "fields, groups, photo URL), its `etag` and `updated_at`. `raw` adds Google's own "
            f"person object. {UNTRUSTED_NOTE}"
        ),
        annotations=READ,
    )
    async def get_contact(
        resource_name: PersonResourceName,
        account: AccountArg = None,
        raw: Annotated[
            bool, Field(description="Also return the raw People API object (with metadata).")
        ] = False,
    ) -> Contact:
        acct = registry.resolve(account)
        person = await acct.people.get_person(resource_name, READ_FIELDS)
        groups = await acct.group_names(best_effort=True)
        return person_to_contact(person, group_names=groups, account=acct.alias, raw=raw)
