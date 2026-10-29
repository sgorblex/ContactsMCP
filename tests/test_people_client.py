"""PeopleClient against a mocked People API (respx): auth, retries, error mapping."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx

from contacts_mcp.config import GoogleClientConfig
from contacts_mcp.errors import (
    EtagConflictError,
    GoogleApiError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)
from contacts_mcp.fields import PERSON_FIELDS, mask
from contacts_mcp.google.oauth import TOKEN_URL, TokenProvider
from contacts_mcp.google.people import BATCH_LIMIT, GROUP_FIELDS, PeopleClient, batch_item_error
from tests.conftest import ACCESS_TOKEN, PEOPLE, person

type Built = tuple[PeopleClient, list[float]]


@pytest.fixture
def build(http: httpx.AsyncClient, google_client: GoogleClientConfig) -> Callable[..., Built]:
    def factory(*, max_retries: int = 4) -> Built:
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            delays.append(delay)

        tokens = TokenProvider("work", google_client, "refresh-work", http)
        return PeopleClient(http, tokens, max_retries=max_retries, sleep=sleep), delays

    return factory


def error_body(
    status: int, text: str, *, google_status: str = "X", reason: str | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": status, "message": text, "status": google_status}
    if reason:
        err["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return {"error": err}


# --- auth -------------------------------------------------------------------------------


async def test_request_carries_the_bearer_token(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    client, _ = build()
    route = google.get(f"{PEOPLE}/people/c1").respond(200, json=person("people/c1"))
    result = await client.get_person("people/c1")
    assert result["resourceName"] == "people/c1"
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert "personFields=names" in str(route.calls.last.request.url)


async def test_401_forces_one_refresh_then_retries(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    tokens = iter(["tok-1", "tok-2", "tok-3"])
    token_route = google.post(TOKEN_URL).mock(
        side_effect=lambda _: httpx.Response(
            200, json={"access_token": next(tokens), "expires_in": 3600}
        )
    )
    route = google.get(f"{PEOPLE}/people/c1").mock(
        side_effect=[httpx.Response(401, json={}), httpx.Response(200, json={"ok": True})]
    )
    client, delays = build()
    assert await client.request("GET", "people/c1") == {"ok": True}
    assert token_route.call_count == 2
    sent = [c.request.headers["Authorization"] for c in route.calls]
    assert sent == ["Bearer tok-1", "Bearer tok-2"]
    assert delays == []


async def test_a_second_401_is_an_error_not_a_loop(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    token_route = google.post(TOKEN_URL).respond(
        200, json={"access_token": "t", "expires_in": 3600}
    )
    route = google.get(f"{PEOPLE}/people/c1").respond(401, json={})
    client, _ = build()
    with pytest.raises(GoogleApiError, match="rejected the access token"):
        await client.request("GET", "people/c1")
    assert route.call_count == 2
    assert token_route.call_count == 2


# --- retries ----------------------------------------------------------------------------


async def test_429_and_5xx_are_retried_with_backoff(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.get(f"{PEOPLE}/people/me/connections").mock(
        side_effect=[
            httpx.Response(429, json={}),
            httpx.Response(503, json={}),
            httpx.Response(500, json={}),
            httpx.Response(200, json={"connections": []}),
        ]
    )
    client, delays = build()
    assert await client.list_connections(page_size=5) == {"connections": []}
    assert route.call_count == 4
    assert len(delays) == 3
    # Exponential with jitter: each delay is within [base/2, base] with base 0.5 * 2**n.
    for n, delay in enumerate(delays):
        base = 0.5 * 2**n
        assert base / 2 <= delay <= base


async def test_retry_after_seconds_is_respected(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    google.get(f"{PEOPLE}/people/c1").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={}),
            httpx.Response(200, json={}),
        ]
    )
    client, delays = build()
    await client.request("GET", "people/c1")
    assert delays == [7.0]


async def test_retry_after_http_date_is_respected(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    google.get(f"{PEOPLE}/people/c1").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": when}, json={}),
            httpx.Response(200, json={}),
        ]
    )
    client, delays = build()
    await client.request("GET", "people/c1")
    [delay] = delays
    assert 25 <= delay <= 30


async def test_retry_after_is_capped_by_max_backoff(
    google: respx.MockRouter, http: httpx.AsyncClient, google_client: GoogleClientConfig
) -> None:
    google.get(f"{PEOPLE}/people/c1").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3600"}, json={}),
            httpx.Response(200, json={}),
        ]
    )
    delays: list[float] = []

    async def sleep(d: float) -> None:
        delays.append(d)

    tokens = TokenProvider("work", google_client, "r", http)
    client = PeopleClient(http, tokens, max_backoff=30.0, sleep=sleep)
    await client.request("GET", "people/c1")
    assert delays == [30.0]


async def test_retries_are_bounded(google: respx.MockRouter, build: Callable[..., Built]) -> None:
    route = google.get(f"{PEOPLE}/people/c1").respond(503, text="<html>upstream is down</html>")
    client, delays = build(max_retries=2)
    with pytest.raises(RateLimitedError) as excinfo:
        await client.request("GET", "people/c1")
    assert route.call_count == 3  # first try + 2 retries
    assert len(delays) == 2
    assert excinfo.value.status == 503
    assert "upstream is down" not in str(excinfo.value)


async def test_429_after_max_retries_is_rate_limited(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    google.get(f"{PEOPLE}/people/c1").respond(
        429, json=error_body(429, "Quota exceeded", google_status="RESOURCE_EXHAUSTED")
    )
    client, _ = build(max_retries=1)
    with pytest.raises(RateLimitedError, match="429"):
        await client.request("GET", "people/c1")


async def test_transport_errors_are_retried_then_become_google_api_error(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.get(f"{PEOPLE}/people/c1").mock(side_effect=httpx.ConnectError("boom"))
    client, delays = build(max_retries=2)
    with pytest.raises(GoogleApiError, match="Could not reach the People API") as excinfo:
        await client.request("GET", "people/c1")
    assert route.call_count == 3
    assert len(delays) == 2
    assert not isinstance(excinfo.value, RateLimitedError)


async def test_transport_error_then_success(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    google.get(f"{PEOPLE}/people/c1").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"a": 1})]
    )
    client, delays = build()
    assert await client.request("GET", "people/c1") == {"a": 1}
    assert len(delays) == 1


# --- error mapping ----------------------------------------------------------------------


async def test_404_is_not_found(google: respx.MockRouter, build: Callable[..., Built]) -> None:
    google.get(f"{PEOPLE}/people/c404").respond(
        404, json=error_body(404, "Requested entity was not found.", google_status="NOT_FOUND")
    )
    client, _ = build()
    with pytest.raises(NotFoundError, match="people/c404"):
        await client.get_person("people/c404")


async def test_400_etag_message_is_an_etag_conflict(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    google.patch(f"{PEOPLE}/people/c1:updateContact").respond(
        400,
        json=error_body(
            400,
            "Request person.etag is different than the current person.etag. Clear the "
            "person.etag field in the request to overwrite.",
            google_status="FAILED_PRECONDITION",
        ),
    )
    client, _ = build()
    with pytest.raises(EtagConflictError, match="changed by someone else"):
        await client.update_contact("people/c1", {"etag": "old"}, ["names"])


async def test_other_400_is_a_plain_google_api_error(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    google.get(f"{PEOPLE}/people/c1").respond(
        400, json=error_body(400, "Invalid personFields", google_status="INVALID_ARGUMENT")
    )
    client, _ = build()
    with pytest.raises(GoogleApiError, match="HTTP 400") as excinfo:
        await client.get_person("people/c1")
    assert not isinstance(excinfo.value, EtagConflictError)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("SERVICE_DISABLED", "People API is not enabled"),
        ("ACCESS_TOKEN_SCOPE_INSUFFICIENT", "contacts-mcp auth add"),
        ("SOMETHING_ELSE", "Admin console"),
    ],
)
async def test_403_is_permission_denied_with_an_actionable_message(
    google: respx.MockRouter, build: Callable[..., Built], reason: str, expected: str
) -> None:
    google.get(f"{PEOPLE}/people/c1").respond(
        403,
        json=error_body(
            403, "secret detail from google", google_status="PERMISSION_DENIED", reason=reason
        ),
    )
    client, _ = build()
    with pytest.raises(PermissionDeniedError, match=expected) as excinfo:
        await client.get_person("people/c1")
    assert excinfo.value.reason == reason
    assert "secret detail" not in str(excinfo.value)


async def test_error_messages_never_contain_the_token_or_the_raw_body(
    google: respx.MockRouter, build: Callable[..., Built], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    google.get(f"{PEOPLE}/people/c1").respond(
        500, json=error_body(500, "SECRET-BODY", google_status="INTERNAL")
    )
    google.get(f"{PEOPLE}/people/c2").respond(418, text="<html>SECRET-BODY</html>")
    google.get(f"{PEOPLE}/people/c3").respond(
        400,
        json={
            "error": {"message": "short reason", "status": "INVALID_ARGUMENT", "x": "SECRET-BODY"}
        },
    )
    client, _ = build(max_retries=1)
    for rn in ("people/c1", "people/c2", "people/c3"):
        with pytest.raises(GoogleApiError) as excinfo:
            await client.get_person(rn)
        assert ACCESS_TOKEN not in str(excinfo.value)
        assert "SECRET-BODY" not in str(excinfo.value)
    # Only the short Google message of a 4xx is passed on, never the body as a whole.
    with pytest.raises(GoogleApiError, match="short reason"):
        await client.get_person("people/c3")
    assert ACCESS_TOKEN not in caplog.text
    assert "SECRET-BODY" not in caplog.text


async def test_unreadable_success_body_is_a_google_api_error(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    google.get(f"{PEOPLE}/people/c1").respond(200, text="not json")
    client, _ = build()
    with pytest.raises(GoogleApiError, match="unreadable"):
        await client.get_person("people/c1")


async def test_empty_body_is_an_empty_dict(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    google.delete(f"{PEOPLE}/people/c1:deleteContact").respond(200)
    client, _ = build()
    assert await client.request("DELETE", "people/c1:deleteContact") == {}


# --- endpoints --------------------------------------------------------------------------


async def test_batch_get_chunks_at_200_and_returns_found_persons(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        names = request.url.params.get_list("resourceNames")
        return httpx.Response(
            200,
            json={
                "responses": [
                    {"requestedResourceName": n, "person": {"resourceName": n}}
                    if not n.endswith("0")
                    else {"requestedResourceName": n, "status": {"code": 5}}
                    for n in names
                ]
            },
        )

    route = google.get(f"{PEOPLE}/people:batchGet").mock(side_effect=respond)
    names = [f"people/c{i}" for i in range(450)]
    client, _ = build()
    found = await client.batch_get(names)
    sizes = [len(c.request.url.params.get_list("resourceNames")) for c in route.calls]
    assert sizes == [BATCH_LIMIT, BATCH_LIMIT, 50]
    assert len(found) == 450 - 45  # names ending in 0 were not found
    assert all(c.request.url.params["personFields"] for c in route.calls)


async def test_batch_get_of_nothing_makes_no_request(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    client, _ = build()
    assert await client.batch_get([]) == []
    assert len(google.calls) == 0


async def test_list_groups_follows_pagination(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("pageToken") == "p2":
            return httpx.Response(
                200, json={"contactGroups": [{"resourceName": "contactGroups/b"}]}
            )
        return httpx.Response(
            200,
            json={"contactGroups": [{"resourceName": "contactGroups/a"}], "nextPageToken": "p2"},
        )

    route = google.get(f"{PEOPLE}/contactGroups").mock(side_effect=respond)
    client, _ = build()
    groups = await client.list_groups()
    assert [g["resourceName"] for g in groups] == ["contactGroups/a", "contactGroups/b"]
    assert route.call_count == 2
    assert route.calls[0].request.url.params["groupFields"] == "name,groupType,memberCount,metadata"


async def test_list_connections_sends_paging_and_sort(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.get(f"{PEOPLE}/people/me/connections").respond(200, json={})
    client, _ = build()
    await client.list_connections(page_size=7, page_token="tok", sort_order="FIRST_NAME_ASCENDING")
    params = route.calls.last.request.url.params
    assert params["pageSize"] == "7"
    assert params["pageToken"] == "tok"
    assert params["sortOrder"] == "FIRST_NAME_ASCENDING"


async def test_update_contact_sends_etag_and_only_the_given_mask(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.patch(f"{PEOPLE}/people/c1:updateContact").respond(200, json=person("people/c1"))
    client, _ = build()
    await client.update_contact("people/c1", {"etag": "e1", "names": []}, ["names"])
    request = route.calls.last.request
    assert request.url.params["updatePersonFields"] == "names"
    assert json.loads(request.content)["etag"] == "e1"


# -- writes are not repeated when Google may have applied them --------------------------


async def test_write_is_not_retried_after_a_server_error(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.post(f"{PEOPLE}/people:createContact").mock(return_value=httpx.Response(503))
    client, delays = build()
    with pytest.raises(GoogleApiError, match="may or may not have been applied") as excinfo:
        await client.create_contact({"names": [{"givenName": "Ada"}]})
    assert not isinstance(excinfo.value, RateLimitedError)
    assert route.call_count == 1
    assert delays == []


async def test_write_is_not_retried_after_a_read_timeout(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.delete(f"{PEOPLE}/people/c1:deleteContact").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    client, _ = build()
    with pytest.raises(GoogleApiError, match="may or may not have been applied"):
        await client.delete_contact("people/c1")
    assert route.call_count == 1


@pytest.mark.parametrize(
    "not_sent",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")],
)
async def test_write_is_retried_when_it_was_never_sent_or_rate_limited(
    google: respx.MockRouter, build: Callable[..., Built], not_sent: httpx.TransportError
) -> None:
    route = google.post(f"{PEOPLE}/people:createContact").mock(
        side_effect=[
            not_sent,
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json=person()),
        ]
    )
    client, delays = build()
    created = await client.create_contact({"names": [{"givenName": "Ada"}]})
    assert created["resourceName"] == "people/c1"
    assert route.call_count == 3
    assert delays[1] == 1.0


async def test_a_400_about_the_etag_that_is_not_a_conflict_is_not_one(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    google.patch(f"{PEOPLE}/people/c1:updateContact").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "Request must set person.etag.",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
    )
    client, _ = build()
    with pytest.raises(GoogleApiError) as info:
        await client.update_contact("people/c1", {"names": []}, ["names"])
    assert not isinstance(info.value, EtagConflictError)


async def test_a_write_refreshes_the_token_once_on_401(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.post(f"{PEOPLE}/people:createContact").mock(
        side_effect=[httpx.Response(401, json={}), httpx.Response(200, json=person())]
    )
    client, _ = build()
    await client.create_contact({"names": [{"givenName": "Ada"}]})
    assert route.call_count == 2


# -- the request each write endpoint sends ----------------------------------------------


async def test_batch_create_sends_contacts_and_returns_every_response_in_order(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    failed = {"httpStatusCode": 400, "status": {"code": 3, "message": "bad\u200b phone"}}
    route = google.post(f"{PEOPLE}/people:batchCreateContacts").respond(
        200, json={"createdPeople": [{"person": person("people/c1")}, failed]}
    )
    client, _ = build()
    responses = await client.batch_create_contacts(
        [{"names": [{"givenName": "A"}]}, {"names": [{"givenName": "B"}]}],
        person_fields=("names",),
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "contacts": [
            {"contactPerson": {"names": [{"givenName": "A"}]}},
            {"contactPerson": {"names": [{"givenName": "B"}]}},
        ],
        "readMask": "names",
    }
    assert [batch_item_error(r) for r in responses] == [None, "HTTP 400: bad phone"]


async def test_batch_update_sends_the_mask_and_returns_responses_by_name(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.post(f"{PEOPLE}/people:batchUpdateContacts").respond(
        200,
        json={
            "updateResult": {
                "people/c1": {"person": person("people/c1")},
                "people/c2": {"status": {"code": 9}},
            }
        },
    )
    client, _ = build()
    persons = {"people/c1": {"etag": "e1"}, "people/c2": {"etag": "e2"}}
    responses = await client.batch_update_contacts(
        persons, ["names", "emailAddresses"], person_fields=("names",)
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {"contacts": persons, "updateMask": "names,emailAddresses", "readMask": "names"}
    assert batch_item_error(responses["people/c1"]) is None
    assert batch_item_error(responses["people/c2"]) == "failed"


@pytest.mark.parametrize("method", ["batch_create_contacts", "batch_update_contacts"])
async def test_batch_writes_refuse_more_than_the_limit(
    google: respx.MockRouter, build: Callable[..., Built], method: str
) -> None:
    client, _ = build()
    persons: Any = [{}] * (BATCH_LIMIT + 1)
    if method == "batch_update_contacts":
        with pytest.raises(GoogleApiError, match="At most 200"):
            await client.batch_update_contacts(
                {f"people/c{i}": {} for i in range(BATCH_LIMIT + 1)}, ["names"]
            )
    else:
        with pytest.raises(GoogleApiError, match="At most 200"):
            await client.batch_create_contacts(persons)
    assert not google.calls


async def test_group_writes_send_the_expected_requests(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    create = google.post(f"{PEOPLE}/contactGroups").respond(200, json={})
    update = google.put(f"{PEOPLE}/contactGroups/g1").respond(200, json={})
    delete = google.delete(f"{PEOPLE}/contactGroups/g1").respond(200)
    modify = google.post(f"{PEOPLE}/contactGroups/g1/members:modify").respond(200, json={})
    client, _ = build()

    await client.create_group("Friends")
    assert json.loads(create.calls.last.request.content) == {
        "contactGroup": {"name": "Friends"},
        "readGroupFields": GROUP_FIELDS,
    }
    await client.update_group("contactGroups/g1", "Mates", "etag-1")
    assert json.loads(update.calls.last.request.content) == {
        "contactGroup": {"name": "Mates", "etag": "etag-1"},
        "updateGroupFields": "name",
        "readGroupFields": GROUP_FIELDS,
    }
    await client.delete_group("contactGroups/g1", delete_contacts=False)
    assert delete.calls.last.request.url.params["deleteContacts"] == "false"
    await client.modify_group_members("contactGroups/g1", add=["people/c1"], remove=["people/c2"])
    assert json.loads(modify.calls.last.request.content) == {
        "resourceNamesToAdd": ["people/c1"],
        "resourceNamesToRemove": ["people/c2"],
    }


async def test_copy_other_contact_sends_the_copy_and_read_masks(
    google: respx.MockRouter, build: Callable[..., Built]
) -> None:
    route = google.post(f"{PEOPLE}/otherContacts/o1:copyOtherContactToMyContactsGroup").respond(
        200, json=person("people/c9")
    )
    client, _ = build()
    created = await client.copy_other_contact("otherContacts/o1", copy_mask=("names",))
    assert created["resourceName"] == "people/c9"
    body = json.loads(route.calls.last.request.content)
    assert body == {"copyMask": "names", "readMask": mask(PERSON_FIELDS)}
