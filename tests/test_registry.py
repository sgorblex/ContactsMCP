"""AccountRegistry: routing, refusals, reload, warm-up, group-name cache."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
import respx
from pydantic import SecretStr

from contacts_mcp.errors import (
    AccountError,
    NeedsRelinkError,
    PermissionDeniedError,
    ReadOnlyAccountError,
)
from contacts_mcp.google.oauth import TOKEN_URL
from contacts_mcp.registry import AccountRegistry
from tests.conftest import PEOPLE, FakeStore

type MakeRegistry = Callable[..., AccountRegistry]


def empty_query_requests(google: respx.MockRouter, path: str) -> int:
    return sum(
        1
        for call in google.calls
        if call.request.url.path.endswith(path) and call.request.url.params.get("query") == ""
    )


# --- resolve ----------------------------------------------------------------------------


def test_one_account_and_alias_omitted(make_registry: MakeRegistry) -> None:
    registry = make_registry("work")
    assert registry.resolve(None).alias == "work"
    assert registry.resolve("").alias == "work"
    assert registry.resolve("work").alias == "work"
    assert registry.default_alias() == "work"


def test_several_accounts_and_alias_omitted_lists_the_aliases(
    make_registry: MakeRegistry,
) -> None:
    registry = make_registry("work", "home")
    assert registry.default_alias() is None
    with pytest.raises(AccountError, match="pass `account`") as excinfo:
        registry.resolve(None)
    assert "home" in str(excinfo.value)
    assert "work" in str(excinfo.value)


def test_unknown_alias_lists_the_linked_ones(make_registry: MakeRegistry) -> None:
    registry = make_registry("work", "home")
    with pytest.raises(AccountError, match="Unknown account 'nope'") as excinfo:
        registry.resolve("nope")
    assert "work" in str(excinfo.value)
    assert "home" in str(excinfo.value)


def test_alias_matching_ignores_case_and_whitespace(make_registry: MakeRegistry) -> None:
    registry = make_registry("work")
    assert registry.resolve(" WORK ").alias == "work"


def test_no_account_at_all_says_how_to_link_one(make_registry: MakeRegistry) -> None:
    registry = make_registry()
    with pytest.raises(AccountError, match="auth add"):
        registry.resolve(None)
    assert registry.all() == []
    assert registry.default_alias() is None


def test_unreadable_account_is_reported_and_refused(
    make_registry: MakeRegistry, store: FakeStore
) -> None:
    registry = make_registry("work", "home")
    store.break_("home", "not valid JSON")
    registry.reload()

    infos = {a.alias: a.info() for a in registry.all()}
    assert infos["work"].status == "ok"
    assert infos["home"].status == "unreadable"
    assert infos["home"].detail is not None
    assert "not valid JSON" in infos["home"].detail
    assert "contacts-mcp auth add home" in infos["home"].detail
    assert infos["home"].email is None

    assert registry.resolve("work").alias == "work"
    with pytest.raises(AccountError, match="unreadable") as excinfo:
        registry.resolve("home")
    assert "auth add home" in str(excinfo.value)


def test_read_only_account_is_refused_for_writes_only(make_registry: MakeRegistry) -> None:
    registry = make_registry("work", "home", read_only={"home"})
    assert registry.resolve("home").read_only
    assert registry.resolve("home", write=False).alias == "home"
    with pytest.raises(ReadOnlyAccountError, match="set-read-only home off"):
        registry.resolve("home", write=True)
    assert registry.resolve("work", write=True).alias == "work"


async def test_needs_relink_after_invalid_grant(
    google: respx.MockRouter, make_registry: MakeRegistry
) -> None:
    registry = make_registry("work")
    google.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    account = registry.resolve("work")
    with pytest.raises(NeedsRelinkError):
        await account.people.get_me()
    assert account.status == "needs_relink"
    info = account.info()
    assert info.status == "needs_relink"
    assert info.detail is not None
    assert "auth add work" in info.detail
    with pytest.raises(NeedsRelinkError, match="auth add work"):
        registry.resolve("work")


# --- reload -----------------------------------------------------------------------------


async def test_reload_keeps_runtime_state_when_the_refresh_token_is_unchanged(
    google: respx.MockRouter, make_registry: MakeRegistry, store: FakeStore
) -> None:
    google.get(f"{PEOPLE}/people:searchContacts").respond(200, json={})
    registry = make_registry("work")
    before = registry.resolve("work")
    await before.ensure_warm("contacts")
    lock = before.mutation_lock

    store.add("work", read_only=True, email="new@example.com")  # same refresh token
    registry.reload()

    after = registry.resolve("work")
    assert after is before
    assert after.mutation_lock is lock
    assert after.read_only  # the new record's flags apply
    assert after.email == "new@example.com"
    await after.ensure_warm("contacts")
    assert empty_query_requests(google, "people:searchContacts") == 1


async def test_reload_keeps_the_needs_relink_state_until_relinked(
    google: respx.MockRouter, make_registry: MakeRegistry, store: FakeStore
) -> None:
    registry = make_registry("work")
    google.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(NeedsRelinkError):
        await registry.resolve("work").people.get_me()
    registry.reload()
    assert registry.all()[0].status == "needs_relink"


async def test_reload_resets_runtime_state_when_the_refresh_token_changed(
    google: respx.MockRouter, make_registry: MakeRegistry, store: FakeStore
) -> None:
    google.get(f"{PEOPLE}/people:searchContacts").respond(200, json={})
    registry = make_registry("work")
    before = registry.resolve("work")
    await before.ensure_warm("contacts")
    google.get(f"{PEOPLE}/people/me").respond(401, json={})  # forces a refresh...
    google.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})  # ...which fails
    with pytest.raises(NeedsRelinkError):
        await before.people.get_me()
    assert before.status == "needs_relink"

    relinked = store.records["work"].model_copy(  # type: ignore[union-attr]
        update={"refresh_token": SecretStr("refresh-work-relinked")}
    )
    store.records["work"] = relinked
    registry.reload()

    after = registry.all()[0]
    assert after is not before
    assert after.status == "ok"
    assert after.mutation_lock is not before.mutation_lock
    google.post(TOKEN_URL).respond(200, json={"access_token": "t", "expires_in": 3600})
    await after.ensure_warm("contacts")
    assert empty_query_requests(google, "people:searchContacts") == 2  # warmed up again


def test_reload_adds_and_removes_accounts(make_registry: MakeRegistry, store: FakeStore) -> None:
    registry = make_registry("work")
    store.add("home")
    registry.reload()
    assert registry.aliases() == ["home", "work"]
    del store.records["work"]
    registry.reload()
    assert registry.aliases() == ["home"]
    assert registry.default_alias() == "home"


def test_reload_never_raises_when_the_directory_cannot_be_scanned(
    make_registry: MakeRegistry, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = make_registry("work")

    def boom() -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(store, "scan", boom)
    registry.reload()
    assert registry.aliases() == ["work"]  # the previous state is kept


# --- warm-up and group names ------------------------------------------------------------


async def test_warm_up_is_sent_once_per_account_and_kind(
    google: respx.MockRouter, make_registry: MakeRegistry
) -> None:
    google.get(f"{PEOPLE}/people:searchContacts").respond(200, json={})
    google.get(f"{PEOPLE}/otherContacts:search").respond(200, json={})
    registry = make_registry("work", "home")
    for _ in range(3):
        for account in registry.all():
            await account.ensure_warm("contacts")
            await account.ensure_warm("other")
    assert empty_query_requests(google, "people:searchContacts") == 2
    assert empty_query_requests(google, "otherContacts:search") == 2


async def test_concurrent_first_searches_warm_up_once(
    google: respx.MockRouter, make_registry: MakeRegistry
) -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        for _ in range(5):
            await asyncio.sleep(0)
        return httpx.Response(200, json={})

    google.get(f"{PEOPLE}/people:searchContacts").mock(side_effect=slow)
    registry = make_registry("work")
    account = registry.resolve("work")
    await asyncio.gather(*(account.ensure_warm("contacts") for _ in range(6)))
    assert empty_query_requests(google, "people:searchContacts") == 1


async def test_failed_warm_up_is_retried_next_time(
    google: respx.MockRouter, make_registry: MakeRegistry
) -> None:
    route = google.get(f"{PEOPLE}/people:searchContacts").mock(
        side_effect=[httpx.Response(403, json={}), httpx.Response(200, json={})]
    )
    account = make_registry("work").resolve("work")
    with pytest.raises(PermissionDeniedError):
        await account.ensure_warm("contacts")
    await account.ensure_warm("contacts")
    await account.ensure_warm("contacts")
    assert route.call_count == 2


async def test_group_names_are_cached_until_invalidated(
    google: respx.MockRouter, make_registry: MakeRegistry
) -> None:
    route = google.get(f"{PEOPLE}/contactGroups").respond(
        200,
        json={
            "contactGroups": [
                {"resourceName": "contactGroups/1", "name": "Family", "formattedName": "Family"},
                {"resourceName": "contactGroups/myContacts", "formattedName": "My Contacts"},
            ]
        },
    )
    account = make_registry("work").resolve("work")
    names = await account.group_names()
    assert names == {"contactGroups/1": "Family", "contactGroups/myContacts": "My Contacts"}
    await account.group_names()
    assert route.call_count == 1
    account.invalidate_groups()
    await account.group_names()
    assert route.call_count == 2
    await account.group_names(refresh=True)
    assert route.call_count == 3
