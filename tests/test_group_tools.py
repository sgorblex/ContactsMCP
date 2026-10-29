"""Group tools and the group resolver."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from contacts_mcp.errors import InvalidArgumentError, NotFoundError
from contacts_mcp.logs import AUDIT_LOGGER
from contacts_mcp.registry import AccountRegistry
from contacts_mcp.tools.groups import resolve_group
from tests.conftest import PEOPLE
from tests.test_write_tools import GROUPS

DUPLICATED: list[dict[str, Any]] = [
    *GROUPS,
    {"resourceName": "contactGroups/d1", "name": "Work", "groupType": "USER_CONTACT_GROUP"},
    {"resourceName": "contactGroups/d2", "name": "work", "groupType": "USER_CONTACT_GROUP"},
]


@pytest.fixture
def registry(make_registry: Callable[..., AccountRegistry]) -> AccountRegistry:
    return make_registry("work")


@pytest.fixture
def client(
    registry: AccountRegistry, make_client: Callable[[AccountRegistry], Client[Any]]
) -> Client[Any]:
    return make_client(registry)


@pytest.fixture(autouse=True)
def groups_route(google: respx.MockRouter) -> respx.Route:
    return google.get(f"{PEOPLE}/contactGroups").mock(
        return_value=httpx.Response(200, json={"contactGroups": DUPLICATED})
    )


# --- resolver --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("contactGroups/abc123", "contactGroups/abc123"),
        ("climbing", "contactGroups/abc123"),
        ("  CLIMBING ", "contactGroups/abc123"),
        ("myContacts", "contactGroups/myContacts"),
        ("mycontacts", "contactGroups/myContacts"),
        ("My Contacts", "contactGroups/myContacts"),
        ("starred", "contactGroups/starred"),
        ("Friends", "contactGroups/friends"),
    ],
)
def test_resolve_group(ref: str, expected: str) -> None:
    assert resolve_group(DUPLICATED, ref).resource_name == expected


def test_resolve_group_system_flag() -> None:
    assert resolve_group(DUPLICATED, "starred").system is True
    assert resolve_group(DUPLICATED, "climbing").system is False


def test_resolve_group_ambiguous_lists_matches() -> None:
    with pytest.raises(InvalidArgumentError) as err:
        resolve_group(DUPLICATED, "WORK")
    assert "contactGroups/d1" in str(err.value)
    assert "contactGroups/d2" in str(err.value)


def test_resolve_group_unknown() -> None:
    with pytest.raises(InvalidArgumentError, match=r"No group named 'Chess'.*'Climbing'"):
        resolve_group(DUPLICATED, "Chess")
    with pytest.raises(NotFoundError):
        resolve_group(DUPLICATED, "contactGroups/nope")
    with pytest.raises(InvalidArgumentError, match="resource name"):
        resolve_group(DUPLICATED, "contactGroups/bad name!")


# --- tools -----------------------------------------------------------------------------


async def test_list_groups(client: Client[Any]) -> None:
    async with client:
        result = await client.call_tool("list_groups", {})
    assert result.structured_content is not None
    groups = {g["resource_name"]: g for g in result.structured_content["groups"]}
    assert groups["contactGroups/abc123"] == {
        "resource_name": "contactGroups/abc123",
        "name": "Climbing",
        "type": "user",
        "member_count": 3,
    }
    assert groups["contactGroups/starred"]["type"] == "system"


async def test_create_group(google: respx.MockRouter, client: Client[Any]) -> None:
    route = google.post(f"{PEOPLE}/contactGroups").mock(
        return_value=httpx.Response(
            200,
            json={
                "resourceName": "contactGroups/new1",
                "name": "Chess",
                "groupType": "USER_CONTACT_GROUP",
            },
        )
    )
    async with client:
        result = await client.call_tool("create_group", {"name": " Chess "})
        with pytest.raises(ToolError, match="already exists"):
            await client.call_tool("create_group", {"name": "climbing"})
    assert json.loads(route.calls.last.request.content)["contactGroup"] == {"name": "Chess"}
    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["resource_name"] == "contactGroups/new1"


async def test_rename_group(google: respx.MockRouter, client: Client[Any]) -> None:
    google.get(f"{PEOPLE}/contactGroups/abc123").mock(
        return_value=httpx.Response(200, json={**GROUPS[3], "etag": "g-etag", "memberCount": 3})
    )
    put = google.put(f"{PEOPLE}/contactGroups/abc123").mock(
        return_value=httpx.Response(
            200, json={**GROUPS[3], "name": "Bouldering", "formattedName": "Bouldering"}
        )
    )
    async with client:
        result = await client.call_tool(
            "rename_group", {"group": "Climbing", "new_name": "Bouldering"}
        )
    sent = json.loads(put.calls.last.request.content)
    assert sent["contactGroup"] == {"name": "Bouldering", "etag": "g-etag"}
    assert result.structured_content is not None
    assert result.structured_content["name"] == "Bouldering"


@pytest.mark.parametrize("tool", ["rename_group", "delete_group"])
@pytest.mark.parametrize("group", ["myContacts", "starred", "Friends", "contactGroups/friends"])
async def test_system_groups_cannot_be_renamed_or_deleted(
    google: respx.MockRouter, client: Client[Any], tool: str, group: str
) -> None:
    writes = [
        google.put(url__regex=rf"{PEOPLE}/contactGroups/.*"),
        google.delete(url__regex=rf"{PEOPLE}/contactGroups/.*"),
    ]
    args: dict[str, Any] = {"group": group}
    if tool == "rename_group":
        args["new_name"] = "Other"
    async with client:
        with pytest.raises(ToolError, match="system group"):
            await client.call_tool(tool, args)
    assert not any(w.called for w in writes)


@pytest.mark.parametrize("delete_contacts", [False, True])
async def test_delete_group(
    google: respx.MockRouter,
    client: Client[Any],
    delete_contacts: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    members = google.get(f"{PEOPLE}/contactGroups/abc123").respond(
        200, json={"memberResourceNames": ["people/c1", "people/c2"]}
    )
    route = google.delete(f"{PEOPLE}/contactGroups/abc123").mock(
        return_value=httpx.Response(200, json={})
    )
    args: dict[str, Any] = {"group": "contactGroups/abc123"}
    if delete_contacts:
        args["delete_contacts"] = True
    caplog.set_level("INFO", logger=AUDIT_LOGGER)
    async with client:
        result = await client.call_tool("delete_group", args)
    params = route.calls.last.request.url.params
    assert params["deleteContacts"] == ("true" if delete_contacts else "false")
    assert result.structured_content is not None
    assert result.structured_content["contacts_deleted"] == (2 if delete_contacts else 0)
    assert result.structured_content["deleted"]["name"] == "Climbing"
    [line] = [r for r in caplog.records if r.name == AUDIT_LOGGER]
    assert line.contacts == (["people/c1", "people/c2"] if delete_contacts else [])  # type: ignore[attr-defined]
    assert members.called is delete_contacts


async def test_delete_group_is_destructive(client: Client[Any]) -> None:
    async with client:
        tools = {t.name: t for t in await client.list_tools()}
    annotations = tools["delete_group"].annotations
    assert annotations is not None
    assert annotations.destructive_hint is True


async def test_add_to_group(
    google: respx.MockRouter, client: Client[Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    route = google.post(f"{PEOPLE}/contactGroups/starred/members:modify").mock(
        return_value=httpx.Response(200, json={"notFoundResourceNames": ["people/c404"]})
    )
    async with client:
        result = await client.call_tool(
            "add_to_group",
            {"group": "starred", "resource_names": ["people/c1", "people/c404", "people/c1"]},
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"resourceNamesToAdd": ["people/c1", "people/c404"]}
    assert result.structured_content == {
        "group": {"resource_name": "contactGroups/starred", "name": "Starred"},
        "changed": ["people/c1"],
        "not_found": ["people/c404"],
        "could_not_remove": [],
    }
    (line,) = [r for r in caplog.records if r.name == "contacts_mcp.audit"]
    assert line.resource_name == "contactGroups/starred"  # type: ignore[attr-defined]
    assert line.contacts == ["people/c1", "people/c404"]  # type: ignore[attr-defined]


async def test_remove_from_group_reports_last_group(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    route = google.post(f"{PEOPLE}/contactGroups/abc123/members:modify").mock(
        return_value=httpx.Response(
            200, json={"canNotRemoveLastContactGroupResourceNames": ["people/c2"]}
        )
    )
    async with client:
        result = await client.call_tool(
            "remove_from_group",
            {"group": "climbing", "resource_names": ["people/c1", "people/c2"]},
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"resourceNamesToRemove": ["people/c1", "people/c2"]}
    assert result.structured_content is not None
    assert result.structured_content["changed"] == ["people/c1"]
    assert result.structured_content["could_not_remove"] == ["people/c2"]


async def test_members_of_other_system_groups_refused(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    route = google.post(url__regex=rf"{PEOPLE}/contactGroups/.*/members:modify")
    async with client:
        with pytest.raises(ToolError, match="cannot have members changed"):
            await client.call_tool(
                "add_to_group", {"group": "friends", "resource_names": ["people/c1"]}
            )
    assert not route.called


async def test_ambiguous_group_name_is_an_error(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    async with client:
        with pytest.raises(ToolError, match=r"Several groups.*contactGroups/d1"):
            await client.call_tool(
                "add_to_group", {"group": "work", "resource_names": ["people/c1"]}
            )


async def test_group_write_invalidates_cache(
    google: respx.MockRouter, registry: AccountRegistry, client: Client[Any]
) -> None:
    google.post(f"{PEOPLE}/contactGroups/abc123/members:modify").mock(
        return_value=httpx.Response(200, json={})
    )
    account = registry.resolve("work")
    await account.group_names()
    assert account._groups is not None
    async with client:
        await client.call_tool(
            "add_to_group", {"group": "Climbing", "resource_names": ["people/c1"]}
        )
    assert account._groups is None


async def test_group_writes_refused_on_read_only(
    google: respx.MockRouter,
    make_registry: Callable[..., AccountRegistry],
    make_client: Callable[[AccountRegistry], Client[Any]],
) -> None:
    registry = make_registry("home", read_only={"home"})
    async with make_client(registry) as client:
        with pytest.raises(ToolError, match="read-only"):
            await client.call_tool("create_group", {"name": "X"})
        # Reads still work.
        await client.call_tool("list_groups", {})
