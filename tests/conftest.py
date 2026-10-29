"""Shared fixtures: a mocked Google (respx) and a People API person builder.

The People API is mocked at the HTTP layer: tests register routes on ``google``
(a ``respx.MockRouter``) against ``PEOPLE`` (the API base URL). The token endpoint is
pre-mocked to hand out ``ACCESS_TOKEN``; tests that need other token responses override
the route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from contacts_mcp.config import GoogleClientConfig
from contacts_mcp.fields import PEOPLE_API_URL
from contacts_mcp.google.oauth import TOKEN_URL

PEOPLE = PEOPLE_API_URL
ACCESS_TOKEN = "test-access-token"


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
