"""Linked accounts at runtime: routing, per-account state, reload from the store."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Literal

import httpx

from contacts_mcp.accounts import AccountFileError, AccountFileStore, AccountRecord
from contacts_mcp.config import GoogleClientConfig
from contacts_mcp.errors import AccountError, ContactsError, NeedsRelinkError, ReadOnlyAccountError
from contacts_mcp.fields import SUMMARY_FIELDS
from contacts_mcp.google.oauth import TokenProvider
from contacts_mcp.google.people import PeopleClient
from contacts_mcp.models import AccountInfo, AccountStatusName

log = logging.getLogger(__name__)

GROUP_CACHE_SECONDS = 300.0

WarmupKind = Literal["contacts", "other"]
PeopleFactory = Callable[[httpx.AsyncClient, TokenProvider], PeopleClient]


class Account:
    """One linked account: its file record, API client, mutation lock and caches."""

    def __init__(
        self,
        alias: str,
        record: AccountRecord | None,
        people: PeopleClient | None,
        tokens: TokenProvider | None,
        error: str | None = None,
    ) -> None:
        self.alias = alias
        self.record = record
        self.people_client = people
        self.tokens = tokens
        self.error = error
        #: The People API wants a user's mutations sent sequentially.
        self.mutation_lock = asyncio.Lock()
        self._warmed: set[WarmupKind] = set()
        self._warm_lock = asyncio.Lock()
        self._groups: dict[str, str] | None = None
        self._groups_at = 0.0

    @property
    def status(self) -> AccountStatusName:
        if self.record is None or self.people_client is None:
            return "unreadable"
        if self.tokens is not None and self.tokens.invalid:
            return "needs_relink"
        return "ok"

    @property
    def read_only(self) -> bool:
        return self.record.read_only if self.record else False

    @property
    def email(self) -> str | None:
        return self.record.email if self.record else None

    @property
    def people(self) -> PeopleClient:
        """The API client; raises the account's error if it cannot be used."""
        if self.people_client is None or self.record is None:
            raise AccountError(
                f"Account '{self.alias}' is unreadable ({self.error}). Fix or re-link it with "
                f"`contacts-mcp auth add {self.alias}`."
            )
        if self.tokens is not None and self.tokens.invalid:
            raise NeedsRelinkError(self.alias)
        return self.people_client

    def info(self) -> AccountInfo:
        status = self.status
        detail = None
        if status == "unreadable":
            detail = f"{self.error}. Re-link with `contacts-mcp auth add {self.alias}`."
        elif status == "needs_relink":
            detail = (
                "Google rejected the refresh token (revoked or expired). "
                f"Run `contacts-mcp auth add {self.alias}`."
            )
        return AccountInfo(
            alias=self.alias,
            email=self.email,
            read_only=self.read_only,
            status=status,
            detail=detail,
        )

    async def ensure_warm(self, kind: WarmupKind) -> None:
        """Send the warm-up request (empty query) once per process before the first search.

        Google documents that searchContacts / otherContacts:search serve stale or
        empty results until a warm-up request has updated the cache.
        """
        if kind in self._warmed:
            return
        async with self._warm_lock:
            if kind in self._warmed:
                return
            if kind == "contacts":
                await self.people.search_contacts("", page_size=1, read_mask=SUMMARY_FIELDS)
            else:
                await self.people.search_other_contacts("", page_size=1)
            self._warmed.add(kind)

    async def group_names(
        self, *, refresh: bool = False, best_effort: bool = False
    ) -> dict[str, str]:
        """``contactGroups/…`` → display name for every group (cached for a few minutes).

        With ``best_effort``, a failure to list the groups returns the last known names
        (or none: callers then show bare group IDs) instead of failing the caller.
        """
        now = time.monotonic()
        if refresh or self._groups is None or now - self._groups_at > GROUP_CACHE_SECONDS:
            try:
                groups = await self.people.list_groups()
            except ContactsError as e:
                if not best_effort:
                    raise
                log.warning("cannot list contact groups: %s", e, extra={"account": self.alias})
                return self._groups or {}
            self._groups = {
                g["resourceName"]: g.get("formattedName") or g.get("name") or g["resourceName"]
                for g in groups
                if "resourceName" in g
            }
            self._groups_at = now
        return self._groups

    def invalidate_groups(self) -> None:
        """Call after any group write."""
        self._groups = None


class AccountRegistry:
    """Every linked account, re-read from the store on :meth:`reload`."""

    def __init__(
        self,
        store: AccountFileStore,
        google: GoogleClientConfig,
        http: httpx.AsyncClient,
        *,
        people_factory: PeopleFactory | None = None,
    ) -> None:
        self.store = store
        self.google = google
        self.http = http
        self._people_factory: PeopleFactory = people_factory or (lambda h, t: PeopleClient(h, t))
        self._accounts: dict[str, Account] = {}

    def reload(self) -> None:
        """Re-read the account files. Never raises: failures become ``unreadable`` accounts.

        An account whose refresh token is unchanged keeps its runtime state (token
        cache, mutation lock, warm-up); a changed token (re-link) starts fresh.
        """
        try:
            scanned = self.store.scan()
        except OSError as e:
            log.error(  # noqa: TRY400 - the reason is enough
                "cannot scan the accounts directory: %s", e.strerror
            )
            return
        accounts: dict[str, Account] = {}
        for alias, result in sorted(scanned.items()):
            if isinstance(result, AccountFileError):
                accounts[alias] = Account(alias, None, None, None, error=str(result))
                continue
            current = self._accounts.get(alias)
            if (
                current is not None
                and current.record is not None
                and current.record.refresh_token.get_secret_value()
                == result.refresh_token.get_secret_value()
            ):
                current.record = result
                accounts[alias] = current
                continue
            tokens = TokenProvider(
                alias, self.google, result.refresh_token.get_secret_value(), self.http
            )
            accounts[alias] = Account(
                alias, result, self._people_factory(self.http, tokens), tokens
            )
        added = sorted(accounts.keys() - self._accounts.keys())
        removed = sorted(self._accounts.keys() - accounts.keys())
        self._accounts = accounts
        log.info(
            "accounts loaded",
            extra={
                "accounts": sorted(accounts),
                "added": added,
                "removed": removed,
                "unreadable": sorted(a for a, acc in accounts.items() if acc.record is None),
            },
        )

    def all(self) -> list[Account]:
        return list(self._accounts.values())

    def aliases(self) -> list[str]:
        return list(self._accounts)

    def default_alias(self) -> str | None:
        return next(iter(self._accounts)) if len(self._accounts) == 1 else None

    def resolve(self, alias: str | None, *, write: bool = False) -> Account:
        """The account named ``alias`` (or the only one when omitted).

        Raises AccountError for an unknown or omitted-but-ambiguous alias, an unusable
        account, and (with ``write``) ReadOnlyAccountError for a read-only one.
        """
        if not self._accounts:
            raise AccountError(
                "No Google account is linked. Link one on the server with "
                "`contacts-mcp auth add <alias>`."
            )
        if alias is None or alias == "":
            if len(self._accounts) != 1:
                raise AccountError(
                    "Several accounts are linked; pass `account`, one of: "
                    + ", ".join(self._accounts)
                )
            account = next(iter(self._accounts.values()))
        else:
            found = self._accounts.get(alias.strip().lower())
            if found is None:
                raise AccountError(
                    f"Unknown account '{alias}'. Linked accounts: " + ", ".join(self._accounts)
                )
            account = found
        account.people  # noqa: B018 - raises if the account is unusable
        if write and account.read_only:
            raise ReadOnlyAccountError(
                f"Account '{account.alias}' is read-only; writes are refused. The operator "
                f"can change it with `contacts-mcp auth set-read-only {account.alias} off`."
            )
        return account
