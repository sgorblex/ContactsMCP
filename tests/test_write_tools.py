"""Write tools over the MCP client, with the People API mocked by respx."""

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

from contacts_mcp.registry import AccountRegistry
from tests.conftest import PEOPLE, person

GROUPS: list[dict[str, Any]] = [
    {
        "resourceName": "contactGroups/myContacts",
        "name": "myContacts",
        "formattedName": "My Contacts",
        "groupType": "SYSTEM_CONTACT_GROUP",
    },
    {
        "resourceName": "contactGroups/starred",
        "name": "starred",
        "formattedName": "Starred",
        "groupType": "SYSTEM_CONTACT_GROUP",
    },
    {
        "resourceName": "contactGroups/friends",
        "name": "friends",
        "formattedName": "Friends",
        "groupType": "SYSTEM_CONTACT_GROUP",
    },
    {
        "resourceName": "contactGroups/abc123",
        "name": "Climbing",
        "formattedName": "Climbing",
        "groupType": "USER_CONTACT_GROUP",
        "memberCount": 3,
    },
]

ETAG_CONFLICT = httpx.Response(
    400,
    json={
        "error": {
            "code": 400,
            "status": "FAILED_PRECONDITION",
            "message": "Request person.etag is different than the current person.etag. "
            "Clear local cache and get the latest person.",
        }
    },
)


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


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
        return_value=httpx.Response(200, json={"contactGroups": GROUPS})
    )


def echo_update(request: httpx.Request) -> httpx.Response:
    """updateContact answering with the sent person and a new etag."""
    sent = body_of(request)
    return httpx.Response(200, json={**sent, "etag": "etag-new"})


# --- update_contact --------------------------------------------------------------------


async def test_update_adds_phone_and_keeps_the_first(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    current = person(phones=["+39 333 1234567"], emails=["ada@example.com"])
    google.get(f"{PEOPLE}/people/c1").mock(return_value=httpx.Response(200, json=current))
    patch = google.patch(f"{PEOPLE}/people/c1:updateContact").mock(side_effect=echo_update)

    async with client:
        result = await client.call_tool(
            "update_contact",
            {
                "resource_name": "people/c1",
                "phones": {"add": [{"value": "+44 20 7946 0000", "type": "work"}]},
            },
        )

    request = patch.calls.last.request
    assert request.url.params["updatePersonFields"] == "phoneNumbers"
    sent = body_of(request)
    assert sent["etag"] == "etag-1"
    assert sent["metadata"] == current["metadata"]
    assert "emailAddresses" not in sent
    assert "names" not in sent
    assert sent["phoneNumbers"][0] == current["phoneNumbers"][0]
    assert sent["phoneNumbers"][1] == {"value": "+44 20 7946 0000", "type": "work"}

    data = result.structured_content
    assert data is not None
    assert data["etag"] == "etag-new"
    assert data["changes"][0]["field"] == "phones"
    assert len(data["changes"][0]["before"]) == 1
    assert len(data["changes"][0]["after"]) == 2
    assert data["contact"]["phone"] == "+39 333 1234567"


async def test_update_remove_matching_nothing_sends_no_write(
    google: respx.MockRouter, client: Client[Any], caplog: pytest.LogCaptureFixture
) -> None:
    google.get(f"{PEOPLE}/people/c1").mock(
        return_value=httpx.Response(200, json=person(emails=["ada@example.com"]))
    )
    patch = google.patch(f"{PEOPLE}/people/c1:updateContact").mock(side_effect=echo_update)

    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    async with client:
        with pytest.raises(ToolError, match=r"nobody@example\.com.*'ada@example\.com'"):
            await client.call_tool(
                "update_contact",
                {"resource_name": "people/c1", "emails": {"remove": ["nobody@example.com"]}},
            )
    assert not patch.called
    audits = [r for r in caplog.records if r.name == "contacts_mcp.audit"]
    assert len(audits) == 1
    assert audits[0].outcome == "refused"  # type: ignore[attr-defined]
    # The refused attempt still names the field it asked to change...
    assert audits[0].fields == ["emailAddresses"]  # type: ignore[attr-defined]
    # ...but no contact values.
    assert "ada@example.com" not in str(vars(audits[0]))


async def test_update_noop_makes_no_patch(google: respx.MockRouter, client: Client[Any]) -> None:
    google.get(f"{PEOPLE}/people/c1").mock(
        return_value=httpx.Response(200, json=person(emails=["ada@example.com"]))
    )
    patch = google.patch(f"{PEOPLE}/people/c1:updateContact").mock(side_effect=echo_update)
    async with client:
        result = await client.call_tool(
            "update_contact",
            {
                "resource_name": "people/c1",
                "emails": {"add": [{"value": "ADA@example.com"}]},
                "name": {"given": "Ada"},
            },
        )
    assert not patch.called
    assert result.structured_content is not None
    assert result.structured_content["changes"] == []
    assert result.structured_content["etag"] == "etag-1"


async def test_update_etag_conflict_retries_on_fresh_person(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    stale = person(etag="etag-1", emails=["ada@example.com"])
    fresh = person(etag="etag-2", emails=["ada@example.com", "added@elsewhere.io"])
    get = google.get(f"{PEOPLE}/people/c1").mock(
        side_effect=[httpx.Response(200, json=stale), httpx.Response(200, json=fresh)]
    )
    responses = iter([ETAG_CONFLICT, None])

    def conflict_then_echo(request: httpx.Request) -> httpx.Response:
        return next(responses) or echo_update(request)

    patch = google.patch(f"{PEOPLE}/people/c1:updateContact").mock(side_effect=conflict_then_echo)
    async with client:
        result = await client.call_tool(
            "update_contact",
            {"resource_name": "people/c1", "emails": {"add": [{"value": "new@x.io"}]}},
        )
    assert get.call_count == 2
    assert patch.call_count == 2
    second = body_of(patch.calls[1].request)
    assert second["etag"] == "etag-2"
    # The same operation re-applied to the fresh person: nothing of theirs is lost.
    assert [e["value"] for e in second["emailAddresses"]] == [
        "ada@example.com",
        "added@elsewhere.io",
        "new@x.io",
    ]
    assert result.structured_content is not None
    assert result.structured_content["etag"] == "etag-new"


async def test_update_second_etag_conflict_fails_clearly(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    google.get(f"{PEOPLE}/people/c1").mock(
        return_value=httpx.Response(200, json=person(emails=["ada@example.com"]))
    )
    patch = google.patch(f"{PEOPLE}/people/c1:updateContact").mock(return_value=ETAG_CONFLICT)
    async with client:
        with pytest.raises(ToolError, match="changed again"):
            await client.call_tool(
                "update_contact",
                {"resource_name": "people/c1", "biography": "notes"},
            )
    assert patch.call_count == 2


async def test_update_refused_on_read_only_account_and_audited(
    google: respx.MockRouter,
    make_registry: Callable[..., AccountRegistry],
    make_client: Callable[[AccountRegistry], Client[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = make_registry("home", read_only={"home"})
    get = google.get(f"{PEOPLE}/people/c1").mock(return_value=httpx.Response(200, json=person()))
    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    async with make_client(registry) as client:
        with pytest.raises(ToolError, match="read-only"):
            await client.call_tool(
                "update_contact",
                {"resource_name": "people/c1", "account": "home", "biography": "x"},
            )
    assert not get.called
    audits = [r for r in caplog.records if r.name == "contacts_mcp.audit"]
    assert len(audits) == 1
    record: Any = audits[0]
    assert record.kind == "audit"
    assert record.outcome == "refused"
    assert record.tool == "update_contact"
    assert record.account == "home"
    assert record.resource_name == "people/c1"


async def test_update_audit_records_field_names(
    google: respx.MockRouter, client: Client[Any], caplog: pytest.LogCaptureFixture
) -> None:
    google.get(f"{PEOPLE}/people/c1").mock(return_value=httpx.Response(200, json=person()))
    google.patch(f"{PEOPLE}/people/c1:updateContact").mock(side_effect=echo_update)
    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    async with client:
        await client.call_tool(
            "update_contact",
            {"resource_name": "people/c1", "biography": "secret notes", "name": {"given": "A"}},
        )
    (record,) = [r for r in caplog.records if r.name == "contacts_mcp.audit"]
    assert record.outcome == "ok"  # type: ignore[attr-defined]
    assert record.fields == ["biographies", "names"]  # type: ignore[attr-defined]
    assert "secret notes" not in str(vars(record))


async def test_unknown_argument_refused_by_name(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    get = google.get(f"{PEOPLE}/people/c1").mock(return_value=httpx.Response(200, json=person()))
    async with client:
        with pytest.raises(ToolError, match="email_addresses"):
            await client.call_tool(
                "update_contact",
                {"resource_name": "people/c1", "email_addresses": {"add": [{"value": "a@b.c"}]}},
            )
        with pytest.raises(ToolError, match="primay"):
            await client.call_tool(
                "update_contact",
                {
                    "resource_name": "people/c1",
                    "emails": {"add": [{"value": "a@b.c", "primay": 1}]},
                },
            )
    assert not get.called


async def test_update_rejects_bad_resource_name(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    async with client:
        with pytest.raises(ToolError, match="resource name"):
            await client.call_tool(
                "update_contact", {"resource_name": "contactGroups/x", "biography": "x"}
            )


# --- create / delete / copy ------------------------------------------------------------


async def test_create_contact_with_groups(google: respx.MockRouter, client: Client[Any]) -> None:
    def created(request: httpx.Request) -> httpx.Response:
        sent = body_of(request)
        return httpx.Response(200, json={**sent, "resourceName": "people/c9", "etag": "e9"})

    create = google.post(f"{PEOPLE}/people:createContact").mock(side_effect=created)
    async with client:
        result = await client.call_tool(
            "create_contact",
            {
                "name": {"given": "Ada", "family": "Lovelace"},
                "emails": [{"value": "ada@example.com", "type": "home"}],
                "groups": ["climbing", "Starred"],
            },
        )
    sent = body_of(create.calls.last.request)
    assert sent["names"] == [{"givenName": "Ada", "familyName": "Lovelace"}]
    assert [
        m["contactGroupMembership"]["contactGroupResourceName"] for m in sent["memberships"]
    ] == [
        "contactGroups/myContacts",
        "contactGroups/abc123",
        "contactGroups/starred",
    ]
    data = result.structured_content
    assert data is not None
    assert data["resource_name"] == "people/c9"
    assert {g["name"] for g in data["groups"]} == {"My Contacts", "Climbing", "Starred"}


async def test_create_contact_refuses_system_group(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    create = google.post(f"{PEOPLE}/people:createContact")
    async with client:
        with pytest.raises(ToolError, match="System group"):
            await client.call_tool(
                "create_contact", {"name": {"given": "Ada"}, "groups": ["friends"]}
            )
    assert not create.called


async def test_create_contact_needs_a_field(google: respx.MockRouter, client: Client[Any]) -> None:
    async with client:
        with pytest.raises(ToolError, match="at least one field"):
            await client.call_tool("create_contact", {})


async def test_delete_contact_returns_summary(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    google.get(f"{PEOPLE}/people/c1").mock(
        return_value=httpx.Response(200, json=person(emails=["ada@example.com"]))
    )
    delete = google.delete(f"{PEOPLE}/people/c1:deleteContact").mock(
        return_value=httpx.Response(200, json={})
    )
    async with client:
        result = await client.call_tool("delete_contact", {"resource_name": "people/c1"})
    assert delete.called
    assert result.structured_content == {
        "deleted": {
            "resource_name": "people/c1",
            "account": "work",
            "display_name": "Ada Lovelace",
            "email": "ada@example.com",
            "phone": None,
            "organization": None,
            "groups": [],
            "updated_at": None,
            "has_notes": None,
            "field_count": None,
        }
    }


def _descriptions(schema: Any) -> list[str]:
    if isinstance(schema, dict):
        own = [schema["description"]] if isinstance(schema.get("description"), str) else []
        return own + [d for v in schema.values() for d in _descriptions(v)]
    if isinstance(schema, list):
        return [d for v in schema for d in _descriptions(v)]
    return []


async def test_list_operations_stay_compact_in_the_schemas(client: Client[Any]) -> None:
    # Repeated per field and per tool, these descriptions cost context on every request:
    # OPS_NOTE says once what the operations do. No $ref either: some clients can't follow it.
    async with client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in ("update_contact", "batch_update_contacts"):
        schema = tools[name].input_schema
        assert "$ref" not in json.dumps(schema), name
    emails = tools["update_contact"].input_schema["properties"]["emails"]
    # Only the type hint of the e-mail model (in add and in replace_all).
    assert set(_descriptions(emails)) == {"e.g. home, work, other, or any label"}


async def test_delete_contact_is_destructive(client: Client[Any]) -> None:
    async with client:
        tools = {t.name: t for t in await client.list_tools()}
    annotations = tools["delete_contact"].annotations
    assert annotations is not None
    assert annotations.destructive_hint is True
    # destructiveHint=false promises additive changes only; these can remove values.
    for name in ("update_contact", "batch_update_contacts", "remove_from_group"):
        hints = tools[name].annotations
        assert hints is not None
        assert hints.destructive_hint is True, name
        assert hints.read_only_hint is False, name
    create = tools["create_contact"].annotations
    assert create is not None
    assert create.destructive_hint is False


async def test_copy_other_contact(
    google: respx.MockRouter, client: Client[Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    copy = google.post(f"{PEOPLE}/otherContacts/c5:copyOtherContactToMyContactsGroup").mock(
        return_value=httpx.Response(200, json=person("people/c77", emails=["x@y.io"]))
    )
    async with client:
        result = await client.call_tool(
            "copy_other_contact_to_contacts", {"resource_name": "otherContacts/c5"}
        )
    assert copy.called
    assert result.structured_content is not None
    assert result.structured_content["resource_name"] == "people/c77"
    [line] = [r for r in caplog.records if r.name == "contacts_mcp.audit"]
    assert line.contacts == ["people/c77"]  # type: ignore[attr-defined]


# --- batch -----------------------------------------------------------------------------


async def test_batch_create(google: respx.MockRouter, client: Client[Any]) -> None:
    def created(request: httpx.Request) -> httpx.Response:
        sent = body_of(request)
        people = [
            {"person": {**c["contactPerson"], "resourceName": f"people/c{n}"}}
            for n, c in enumerate(sent["contacts"])
        ]
        return httpx.Response(200, json={"createdPeople": people})

    route = google.post(f"{PEOPLE}/people:batchCreateContacts").mock(side_effect=created)
    async with client:
        result = await client.call_tool(
            "batch_create_contacts",
            {
                "contacts": [
                    {"name": {"given": "A"}, "groups": ["Climbing"]},
                    {"emails": [{"value": "b@x.io"}]},
                ]
            },
        )
    sent = body_of(route.calls.last.request)
    assert len(sent["contacts"]) == 2
    assert "memberships" not in sent["contacts"][1]["contactPerson"]
    assert result.structured_content is not None
    contacts = result.structured_content["contacts"]
    assert [c["resource_name"] for c in contacts] == ["people/c0", "people/c1"]
    assert contacts[0]["groups"] == ["Climbing"]


async def test_batch_create_reports_the_contacts_google_refused(
    google: respx.MockRouter, client: Client[Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="contacts_mcp.audit")
    google.post(f"{PEOPLE}/people:batchCreateContacts").respond(
        200,
        json={
            "createdPeople": [
                {"person": person("people/c0")},
                {"httpStatusCode": 400, "status": {"code": 3, "message": "Invalid phone."}},
            ]
        },
    )
    async with client:
        result = await client.call_tool(
            "batch_create_contacts",
            {"contacts": [{"name": {"given": "A"}}, {"phones": [{"value": "x"}]}]},
        )
    assert result.structured_content is not None
    assert [c["resource_name"] for c in result.structured_content["contacts"]] == ["people/c0"]
    assert result.structured_content["failures"] == [
        {"item": "contacts[1]", "error": "HTTP 400: Invalid phone."}
    ]
    [line] = [r for r in caplog.records if r.name == "contacts_mcp.audit"]
    assert line.contacts == ["people/c0"]  # type: ignore[attr-defined]
    assert line.detail == "1 of 2 contacts failed"  # type: ignore[attr-defined]


async def test_batch_create_validates_everything_first(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    route = google.post(f"{PEOPLE}/people:batchCreateContacts")
    async with client:
        with pytest.raises(ToolError, match=r"contacts\[1\]"):
            await client.call_tool(
                "batch_create_contacts", {"contacts": [{"name": {"given": "A"}}, {}]}
            )
    assert not route.called


async def test_batch_update_union_mask(google: respx.MockRouter, client: Client[Any]) -> None:
    p1 = person("people/c1", etag="e1", emails=["a@x.io"], phones=["111"])
    p2 = person("people/c2", etag="e2", emails=["b@x.io"], phones=["222"])
    p3 = person("people/c3", etag="e3", emails=["c@x.io"])
    google.get(f"{PEOPLE}/people:batchGet").mock(
        return_value=httpx.Response(200, json={"responses": [{"person": p} for p in (p1, p2, p3)]})
    )

    def updated(request: httpx.Request) -> httpx.Response:
        sent = body_of(request)
        return httpx.Response(
            200,
            json={
                "updateResult": {
                    rn: {"person": {**p, "etag": "new-" + p["etag"]}}
                    for rn, p in sent["contacts"].items()
                }
            },
        )

    route = google.post(f"{PEOPLE}/people:batchUpdateContacts").mock(side_effect=updated)
    async with client:
        result = await client.call_tool(
            "batch_update_contacts",
            {
                "updates": [
                    {"resource_name": "people/c1", "emails": {"add": [{"value": "a2@x.io"}]}},
                    {"resource_name": "people/c2", "phones": {"remove": ["222"]}},
                    # Already there: unchanged, left out of the request.
                    {"resource_name": "people/c3", "emails": {"add": [{"value": "C@x.io"}]}},
                ]
            },
        )
    sent = body_of(route.calls.last.request)
    assert sent["updateMask"] == "emailAddresses,phoneNumbers"
    assert set(sent["contacts"]) == {"people/c1", "people/c2"}
    c1, c2 = sent["contacts"]["people/c1"], sent["contacts"]["people/c2"]
    assert c1["etag"] == "e1"
    assert [e["value"] for e in c1["emailAddresses"]] == ["a@x.io", "a2@x.io"]
    # c1 does not touch phones, which is in the mask: its current value goes unchanged.
    assert c1["phoneNumbers"] == p1["phoneNumbers"]
    assert c2["emailAddresses"] == p2["emailAddresses"]
    assert c2["phoneNumbers"] == []
    assert result.structured_content is not None
    results = result.structured_content["results"]
    assert [r["etag"] for r in results] == ["new-e1", "new-e2", "e3"]
    assert results[2]["changes"] == []
    assert result.structured_content["failures"] == []


async def test_update_contact_takes_set(google: respx.MockRouter, client: Client[Any]) -> None:
    current = person(etag="e1")
    current["userDefined"] = [{"key": "last_reviewed", "value": "2020-01-01"}]
    google.get(f"{PEOPLE}/people/c1").respond(200, json=current)
    route = google.patch(f"{PEOPLE}/people/c1:updateContact").respond(200, json=current)
    pair = {"key": "last_reviewed", "value": "2026-11-06"}
    async with client:
        await client.call_tool(
            "update_contact", {"resource_name": "people/c1", "user_defined": {"set": [pair]}}
        )
    assert body_of(route.calls.last.request)["userDefined"] == [pair]


async def test_batch_set_needs_no_knowledge_of_which_contacts_have_the_key(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    p1 = person("people/c1", etag="e1")
    p1["userDefined"] = [{"key": "last_reviewed", "value": "2020-01-01"}]
    p2 = person("people/c2", etag="e2")
    google.get(f"{PEOPLE}/people:batchGet").respond(
        200, json={"responses": [{"person": p} for p in (p1, p2)]}
    )
    route = google.post(f"{PEOPLE}/people:batchUpdateContacts").respond(
        200,
        json={
            "updateResult": {
                rn: {"person": person(rn, etag="new")} for rn in ("people/c1", "people/c2")
            }
        },
    )
    pair = {"key": "last_reviewed", "value": "2026-11-06"}
    async with client:
        result = await client.call_tool(
            "batch_update_contacts",
            {
                "updates": [
                    {"resource_name": rn, "user_defined": {"set": [pair]}}
                    for rn in ("people/c1", "people/c2")
                ]
            },
        )
    assert result.structured_content is not None
    assert result.structured_content["failures"] == []
    sent = body_of(route.calls.last.request)
    assert sent["updateMask"] == "userDefined"
    assert sent["contacts"]["people/c1"]["userDefined"] == [pair]
    assert sent["contacts"]["people/c2"]["userDefined"] == [pair]


async def test_batch_update_reports_the_contacts_google_refused(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    p1 = person("people/c1", etag="e1", emails=["a@x.io"])
    p2 = person("people/c2", etag="e2", emails=["b@x.io"])
    google.get(f"{PEOPLE}/people:batchGet").respond(
        200, json={"responses": [{"person": p} for p in (p1, p2)]}
    )
    google.post(f"{PEOPLE}/people:batchUpdateContacts").respond(
        200,
        json={
            "updateResult": {
                "people/c1": {"person": {**p1, "etag": "new"}},
                "people/c2": {"httpStatusCode": 400, "status": {"message": "Bad e-mail."}},
            }
        },
    )
    add = {"add": [{"value": "z@x.io"}]}
    async with client:
        result = await client.call_tool(
            "batch_update_contacts",
            {
                "updates": [
                    {"resource_name": "people/c1", "emails": add},
                    {"resource_name": "people/c2", "emails": add},
                ]
            },
        )
    assert result.structured_content is not None
    assert [r["resource_name"] for r in result.structured_content["results"]] == ["people/c1"]
    assert result.structured_content["failures"] == [
        {"item": "people/c2", "error": "HTTP 400: Bad e-mail."}
    ]


async def test_batch_update_validates_before_writing(
    google: respx.MockRouter, client: Client[Any]
) -> None:
    google.get(f"{PEOPLE}/people:batchGet").mock(
        return_value=httpx.Response(
            200,
            json={
                "responses": [
                    {"person": person("people/c1", emails=["a@x.io"])},
                    {"person": person("people/c2", emails=["b@x.io"])},
                ]
            },
        )
    )
    route = google.post(f"{PEOPLE}/people:batchUpdateContacts")
    async with client:
        with pytest.raises(ToolError, match=r"people/c2: .*zzz@x\.io"):
            await client.call_tool(
                "batch_update_contacts",
                {
                    "updates": [
                        {"resource_name": "people/c1", "biography": "ok"},
                        {"resource_name": "people/c2", "emails": {"remove": ["zzz@x.io"]}},
                    ]
                },
            )
    assert not route.called


async def test_batch_update_etag_retry(google: respx.MockRouter, client: Client[Any]) -> None:
    batch_get = google.get(f"{PEOPLE}/people:batchGet").mock(
        side_effect=[
            httpx.Response(200, json={"responses": [{"person": person(etag="old")}]}),
            httpx.Response(200, json={"responses": [{"person": person(etag="fresh")}]}),
        ]
    )
    route = google.post(f"{PEOPLE}/people:batchUpdateContacts").mock(
        side_effect=[
            ETAG_CONFLICT,
            httpx.Response(200, json={"updateResult": {"people/c1": {"person": person()}}}),
        ]
    )
    async with client:
        await client.call_tool(
            "batch_update_contacts",
            {"updates": [{"resource_name": "people/c1", "biography": "x"}]},
        )
    assert batch_get.call_count == 2
    assert body_of(route.calls[1].request)["contacts"]["people/c1"]["etag"] == "fresh"


async def test_batch_update_refuses_duplicates(client: Client[Any]) -> None:
    async with client:
        with pytest.raises(ToolError, match="repeated"):
            await client.call_tool(
                "batch_update_contacts",
                {
                    "updates": [
                        {"resource_name": "people/c1", "biography": "x"},
                        {"resource_name": "people/c1", "biography": "y"},
                    ]
                },
            )
