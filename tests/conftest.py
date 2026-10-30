"""Shared fixtures: a mocked Google (respx), fake accounts and an in-memory MCP client.

The People API is mocked at the HTTP layer: tests register routes on ``google``
(a ``respx.MockRouter``) against ``PEOPLE`` (the API base URL). The token endpoint is
pre-mocked to hand out ``ACCESS_TOKEN``; tests that need other token responses override
the route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from pydantic import SecretStr

from contacts_mcp.accounts import AccountFileError, AccountRecord
from contacts_mcp.config import GoogleClientConfig
from contacts_mcp.fields import PEOPLE_API_URL
from contacts_mcp.google.oauth import TOKEN_URL
from contacts_mcp.google.people import PeopleClient
from contacts_mcp.registry import AccountRegistry

PEOPLE = PEOPLE_API_URL
ACCESS_TOKEN = "test-access-token"


async def _no_sleep(_: float) -> None:
    return None


class FakeStore:
    """An in-memory stand-in for AccountFileStore (only what the registry uses)."""

    def __init__(self) -> None:
        self.records: dict[str, AccountRecord | AccountFileError] = {}

    def add(self, alias: str, *, read_only: bool = False, email: str | None = None) -> None:
        self.records[alias] = AccountRecord(
            alias=alias,
            email=email or f"{alias}@example.com",
            refresh_token=SecretStr(f"refresh-{alias}"),
            scopes=[
                "https://www.googleapis.com/auth/contacts",
                "https://www.googleapis.com/auth/contacts.other.readonly",
            ],
            read_only=read_only,
            linked_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def break_(self, alias: str, reason: str = "invalid JSON") -> None:
        self.records[alias] = AccountFileError(f"accounts/{alias}.json: {reason}")

    def scan(self) -> dict[str, AccountRecord | AccountFileError]:
        return dict(self.records)


@pytest.fixture
def google_client() -> GoogleClientConfig:
    return GoogleClientConfig(client_id="client-id", client_secret=SecretStr("client-secret"))


@pytest.fixture
def google() -> Iterator[respx.MockRouter]:
    """Mocked Google. The token endpoint answers; People API routes are up to the test."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599}
            )
        )
        yield router


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def make_registry(
    store: FakeStore, google_client: GoogleClientConfig, http: httpx.AsyncClient
) -> Callable[..., AccountRegistry]:
    """``make_registry("work", "home", read_only={"home"})`` → a loaded registry."""

    def factory(
        *aliases: str, read_only: frozenset[str] | set[str] = frozenset()
    ) -> AccountRegistry:
        for alias in aliases:
            store.add(alias, read_only=alias in read_only)
        registry = AccountRegistry(
            store,  # type: ignore[arg-type]
            google_client,
            http,
            people_factory=lambda h, t: PeopleClient(h, t, sleep=_no_sleep),
        )
        registry.reload()
        return registry

    return factory


@pytest.fixture
def make_client() -> Callable[[AccountRegistry], Client[Any]]:
    """An in-memory MCP client over a bare server with every tool registered."""

    def factory(registry: AccountRegistry) -> Client[Any]:
        from contacts_mcp.tools import register_all

        mcp = FastMCP("test")
        register_all(mcp, registry)
        return Client(mcp)

    return factory


def person(
    rn: str = "people/c1",
    *,
    etag: str = "etag-1",
    given: str | None = "Ada",
    family: str | None = "Lovelace",
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    groups: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A People API person object as Google returns it (with metadata noise)."""
    src = {"type": "CONTACT", "id": rn.split("/")[-1]}
    p: dict[str, Any] = {
        "resourceName": rn,
        "etag": etag,
        "metadata": {"sources": [{**src, "etag": etag}], "objectType": "PERSON"},
    }
    if given or family:
        display = " ".join(x for x in (given, family) if x)
        p["names"] = [
            {
                "metadata": {"primary": True, "source": src},
                "displayName": display,
                "givenName": given,
                "familyName": family,
                "unstructuredName": display,
            }
        ]
    if emails:
        p["emailAddresses"] = [
            {"metadata": {"primary": i == 0, "source": src}, "value": e, "type": "home"}
            for i, e in enumerate(emails)
        ]
    if phones:
        p["phoneNumbers"] = [
            {"metadata": {"primary": i == 0, "source": src}, "value": ph, "type": "mobile"}
            for i, ph in enumerate(phones)
        ]
    p["memberships"] = [
        {"contactGroupMembership": {"contactGroupResourceName": g}, "metadata": {"source": src}}
        for g in (groups or ["contactGroups/myContacts"])
    ]
    p.update(extra)
    return p
