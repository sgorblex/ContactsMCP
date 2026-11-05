"""The update engine: pure functions over People API person JSON."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from contacts_mcp.errors import InvalidArgumentError
from contacts_mcp.models import (
    Address,
    CustomFieldOps,
    Email,
    Event,
    ListOps,
    NameFields,
    Organization,
    PartialDate,
    Phone,
    Relation,
    Url,
    UserDefined,
)
from contacts_mcp.update_engine import (
    ContactFields,
    ContactOps,
    build_person,
    check_ops,
    plan_update,
)
from tests.conftest import person

SRC = {"type": "CONTACT", "id": "c1"}


def item(primary: bool = False, **fields: Any) -> dict[str, Any]:
    return {"metadata": {"primary": primary, "source": SRC}, **fields}


def ada(**extra: Any) -> dict[str, Any]:
    return person(
        emails=["ada@example.com", "Countess@Example.org"],
        phones=["+39 333 123 4567", "02 1234 5678"],
        **extra,
    )


def setting(key: str = "last_reviewed", value: str = "2026-11-06") -> ContactOps:
    return ContactOps(user_defined=CustomFieldOps(set=[UserDefined(key=key, value=value)]))


# --- emails / generic list ops ---------------------------------------------------------


def test_add_keeps_existing_items_verbatim() -> None:
    p = ada()
    p["emailAddresses"][1]["formattedType"] = "Home"
    p["emailAddresses"][1]["displayName"] = "unknown sub-field"
    before = copy.deepcopy(p)
    plan = plan_update(p, ContactOps(emails=ListOps(add=[Email(value="new@x.io", type="work")])))
    assert p == before  # input untouched
    assert plan.update_fields == ["emailAddresses"]
    new = plan.fields["emailAddresses"]
    assert new[:2] == before["emailAddresses"]
    assert new[2] == {"value": "new@x.io", "type": "work"}
    change = plan.changes[0]
    assert change.field == "emails"
    assert isinstance(change.after, list)
    assert [e["value"] for e in change.after if isinstance(e, dict)] == [
        "ada@example.com",
        "Countess@Example.org",
        "new@x.io",
    ]


def test_remove_email_is_case_insensitive() -> None:
    plan = plan_update(ada(), ContactOps(emails=ListOps(remove=["  countess@example.ORG "])))
    assert [e["value"] for e in plan.fields["emailAddresses"]] == ["ada@example.com"]


def test_remove_matching_nothing_lists_existing_values() -> None:
    with pytest.raises(InvalidArgumentError) as err:
        plan_update(ada(), ContactOps(emails=ListOps(remove=["nobody@example.com"])))
    msg = str(err.value)
    assert "nobody@example.com" in msg
    assert "'ada@example.com'" in msg
    assert "'Countess@Example.org'" in msg


def test_every_problem_is_reported_at_once() -> None:
    ops = ContactOps(
        emails=ListOps(remove=["a@b.c"]),
        phones=ListOps(remove=["999"]),
    )
    with pytest.raises(InvalidArgumentError) as err:
        plan_update(ada(), ops)
    assert "emails:" in str(err.value)
    assert "phones:" in str(err.value)


def test_add_existing_email_is_a_noop() -> None:
    plan = plan_update(ada(), ContactOps(emails=ListOps(add=[Email(value="ADA@example.com")])))
    assert plan.update_fields == []
    assert plan.changes == []


def test_add_existing_email_with_another_type_changes_its_type() -> None:
    p = ada()
    p["emailAddresses"][0]["formattedType"] = "Home"
    plan = plan_update(
        p, ContactOps(emails=ListOps(add=[Email(value="ADA@example.com", type="work")]))
    )
    assert plan.update_fields == ["emailAddresses"]
    new = plan.fields["emailAddresses"]
    assert len(new) == 2  # no second copy of the address
    assert new[0]["value"] == "ada@example.com"  # the existing spelling is kept
    assert new[0]["type"] == "work"
    assert "formattedType" not in new[0]  # Google recomputes the label
    assert new[0]["metadata"] == p["emailAddresses"][0]["metadata"]
    assert new[1] == p["emailAddresses"][1]
    (change,) = plan.changes
    assert change.field == "emails"
    assert change.before[0]["type"] == "home"  # type: ignore[index,call-overload]
    assert change.after[0]["type"] == "work"  # type: ignore[index,call-overload]


def test_add_existing_email_with_the_same_type_in_another_case_is_a_noop() -> None:
    ops = ContactOps(emails=ListOps(add=[Email(value="ada@example.com", type="HOME")]))
    assert plan_update(ada(), ops).update_fields == []


def test_add_existing_phone_by_digits_with_another_type_changes_its_type() -> None:
    ops = ContactOps(phones=ListOps(add=[Phone(value="+393331234567", type="work")]))
    plan = plan_update(ada(), ops)
    new = plan.fields["phoneNumbers"]
    assert [n["value"] for n in new] == ["+39 333 123 4567", "02 1234 5678"]
    assert new[0]["type"] == "work"


def test_retype_compares_with_the_label_when_the_item_has_no_type() -> None:
    p = ada()
    del p["emailAddresses"][0]["type"]
    p["emailAddresses"][0]["formattedType"] = "Work"
    same = ContactOps(emails=ListOps(add=[Email(value="ada@example.com", type="work")]))
    assert plan_update(p, same).update_fields == []
    other = ContactOps(emails=ListOps(add=[Email(value="ada@example.com", type="home")]))
    new = plan_update(p, other).fields["emailAddresses"]
    assert (new[0]["type"], "formattedType" in new[0]) == ("home", False)


def test_add_existing_url_with_another_type_changes_its_type() -> None:
    p = ada(urls=[item(value="https://example.com", type="blog")])
    ops = ContactOps(urls=ListOps(add=[Url(value="https://EXAMPLE.com", type="homePage")]))
    new = plan_update(p, ops).fields["urls"]
    assert new == [{**p["urls"][0], "type": "homePage"}]


def test_retyped_value_marked_primary_becomes_the_primary() -> None:
    added = Email(value="countess@example.org", type="work", primary=True)
    new = plan_update(ada(), ContactOps(emails=ListOps(add=[added]))).fields["emailAddresses"]
    assert [e["value"] for e in new] == ["Countess@Example.org", "ada@example.com"]
    assert new[0]["type"] == "work"
    assert new[0]["metadata"]["sourcePrimary"] is True
    assert new[1]["metadata"]["sourcePrimary"] is False


def test_retype_changes_the_contacts_own_value_not_the_profiles() -> None:
    p = ada()
    profile = {"value": "ada@example.com", "type": "home", "metadata": {"source": PROFILE}}
    p["emailAddresses"].append(profile)
    ops = ContactOps(emails=ListOps(add=[Email(value="ada@example.com", type="work")]))
    new = plan_update(p, ops).fields["emailAddresses"]
    assert new[0]["type"] == "work"
    assert new[2] == profile


def test_replace_all_then_set_primary_on_the_new_list() -> None:
    ops = ContactOps(
        emails=ListOps(
            replace_all=[Email(value="one@x.io"), Email(value="two@x.io")],
            set_primary="TWO@x.io",
        )
    )
    new = plan_update(ada(), ops).fields["emailAddresses"]
    assert [e["value"] for e in new] == ["two@x.io", "one@x.io"]
    assert new[0]["metadata"] == {"primary": True, "sourcePrimary": True}
    assert "metadata" not in new[1]


def test_replace_all_empty_clears() -> None:
    plan = plan_update(ada(), ContactOps(emails=ListOps(replace_all=[])))
    assert plan.fields["emailAddresses"] == []
    assert plan.changes[0].after == []


def test_set_primary_moves_first_and_flips_flags() -> None:
    p = ada()
    plan = plan_update(p, ContactOps(emails=ListOps(set_primary="countess@example.org")))
    new = plan.fields["emailAddresses"]
    assert [e["value"] for e in new] == ["Countess@Example.org", "ada@example.com"]
    assert new[0]["metadata"]["primary"] is True
    assert new[0]["metadata"]["sourcePrimary"] is True
    assert new[0]["metadata"]["source"] == p["emailAddresses"][1]["metadata"]["source"]
    assert new[1]["metadata"]["primary"] is False
    assert new[1]["metadata"]["sourcePrimary"] is False
    assert new[1]["value"] == "ada@example.com"


def test_set_primary_on_current_primary_is_a_noop() -> None:
    plan = plan_update(ada(), ContactOps(emails=ListOps(set_primary="ada@example.com")))
    assert plan.update_fields == []


def test_set_primary_must_match_exactly_one() -> None:
    with pytest.raises(InvalidArgumentError, match="matches no value"):
        plan_update(ada(), ContactOps(emails=ListOps(set_primary="x@y.z")))
    p = person(emails=["a@x.io", "A@X.io"])
    with pytest.raises(InvalidArgumentError, match="matches 2 values"):
        plan_update(p, ContactOps(emails=ListOps(set_primary="a@x.io")))


def test_added_value_marked_primary_becomes_primary() -> None:
    ops = ContactOps(emails=ListOps(add=[Email(value="main@x.io", primary=True)]))
    new = plan_update(ada(), ops).fields["emailAddresses"]
    assert new[0] == {"value": "main@x.io", "metadata": {"primary": True, "sourcePrimary": True}}
    assert new[1]["metadata"]["primary"] is False
    assert new[2]["metadata"]["primary"] is False


def test_two_primaries_refused() -> None:
    ops = ContactOps(
        emails=ListOps(
            add=[Email(value="a@x.io", primary=True), Email(value="b@x.io", primary=True)]
        )
    )
    with pytest.raises(InvalidArgumentError, match="at most one"):
        plan_update(ada(), ops)


def test_add_and_remove_together() -> None:
    ops = ContactOps(
        emails=ListOps(add=[Email(value="new@x.io")], remove=["ada@example.com"]),
    )
    new = plan_update(ada(), ops).fields["emailAddresses"]
    assert [e["value"] for e in new] == ["Countess@Example.org", "new@x.io"]


# --- phones ----------------------------------------------------------------------------


def test_remove_phone_by_digits() -> None:
    plan = plan_update(ada(), ContactOps(phones=ListOps(remove=["+39-333-1234567"])))
    assert [p["value"] for p in plan.fields["phoneNumbers"]] == ["02 1234 5678"]


def test_remove_phone_by_canonical_form() -> None:
    p = person(phones=["333 123 4567"])
    p["phoneNumbers"][0]["canonicalForm"] = "+393331234567"
    plan = plan_update(p, ContactOps(phones=ListOps(remove=["+39 3331234567"])))
    assert plan.fields["phoneNumbers"] == []


def test_add_phone_keeps_first_verbatim() -> None:
    p = ada()
    ops = ContactOps(phones=ListOps(add=[Phone(value="+44 20 7946 0000", type="work")]))
    new = plan_update(p, ops).fields["phoneNumbers"]
    assert new[:2] == p["phoneNumbers"]
    assert new[2] == {"value": "+44 20 7946 0000", "type": "work"}


# --- other multi-valued fields ---------------------------------------------------------


def test_addresses_match_by_formatted_text() -> None:
    addr = item(
        True,
        formattedValue="Via Roma 1\n20100 Milano\nItaly",
        streetAddress="Via Roma 1",
        city="Milano",
    )
    p = person(addresses=[addr])
    plan = plan_update(p, ContactOps(addresses=ListOps(remove=["via roma 1 20100 milano italy"])))
    assert plan.fields["addresses"] == []

    ops = ContactOps(addresses=ListOps(add=[Address(street="Main St 2", city="Turin")]))
    new = plan_update(p, ops).fields["addresses"]
    assert new == [addr, {"streetAddress": "Main St 2", "city": "Turin"}]


def test_organizations_match_by_name() -> None:
    p = person(organizations=[item(True, name="Analytical Engines", title="Programmer")])
    plan = plan_update(p, ContactOps(organizations=ListOps(remove=["analytical engines"])))
    assert plan.fields["organizations"] == []
    ops = ContactOps(organizations=ListOps(add=[Organization(name="ACME", title="CTO")]))
    assert plan_update(p, ops).fields["organizations"][1] == {"name": "ACME", "title": "CTO"}


def test_set_adds_a_new_key_and_keeps_the_others() -> None:
    p = person(userDefined=[item(False, key="other", value="keep")])
    plan = plan_update(p, setting())
    assert plan.fields["userDefined"] == [
        p["userDefined"][0],
        {"key": "last_reviewed", "value": "2026-11-06"},
    ]


def test_set_replaces_the_value_and_keeps_place_metadata_and_spelling() -> None:
    p = person(
        userDefined=[
            item(False, key="Last_Reviewed", value="2020-01-01"),
            item(False, key="other", value="keep"),
        ]
    )
    plan = plan_update(p, setting())
    assert plan.fields["userDefined"] == [
        {**p["userDefined"][0], "value": "2026-11-06"},
        p["userDefined"][1],
    ]
    assert plan.changes[0].field == "user_defined"


def test_set_collapses_a_repeated_key() -> None:
    p = person(
        userDefined=[
            item(False, key="last_reviewed", value="2020-01-01"),
            item(False, key="other", value="keep"),
            item(False, key="last_reviewed", value="2021-01-01"),
        ]
    )
    plan = plan_update(p, setting())
    assert [(i["key"], i["value"]) for i in plan.fields["userDefined"]] == [
        ("last_reviewed", "2026-11-06"),
        ("other", "keep"),
    ]


def test_set_of_the_current_value_writes_nothing() -> None:
    p = person(userDefined=[item(False, key="last_reviewed", value="2026-11-06")])
    assert plan_update(p, setting()).update_fields == []


def test_set_still_removes_a_duplicate_of_the_current_value() -> None:
    p = person(
        userDefined=[
            item(False, key="last_reviewed", value="2026-11-06"),
            item(False, key="last_reviewed", value="2026-11-06"),
        ]
    )
    assert len(plan_update(p, setting()).fields["userDefined"]) == 1


def test_set_works_alongside_add_and_remove_of_other_keys() -> None:
    p = person(userDefined=[item(False, key="gone", value="x")])
    ops = ContactOps(
        user_defined=CustomFieldOps(
            remove=["gone"],
            add=[UserDefined(key="new", value="1")],
            set=[UserDefined(key="last_reviewed", value="2026-11-06")],
        )
    )
    assert [i["key"] for i in plan_update(p, ops).fields["userDefined"]] == [
        "new",
        "last_reviewed",
    ]


def test_set_leaves_a_profile_value_and_adds_the_contacts_own() -> None:
    profile = {"metadata": {"source": PROFILE}, "key": "last_reviewed", "value": "2020-01-01"}
    p = person(userDefined=[profile])
    plan = plan_update(p, setting())
    assert plan.fields["userDefined"] == [
        profile,
        {"key": "last_reviewed", "value": "2026-11-06"},
    ]


def test_set_refuses_a_blank_key() -> None:
    with pytest.raises(InvalidArgumentError, match="not blank"):
        plan_update(person(), setting(key="   "))


@pytest.mark.parametrize(
    "ops",
    [
        CustomFieldOps(
            set=[UserDefined(key="a", value="1"), UserDefined(key=" A ", value="2")],
        ),
        CustomFieldOps(
            add=[UserDefined(key="a", value="1")],
            set=[UserDefined(key="A", value="2")],
        ),
        CustomFieldOps(remove=["a"], set=[UserDefined(key="A", value="2")]),
    ],
)
def test_set_refuses_a_repeated_or_conflicting_key(ops: CustomFieldOps) -> None:
    with pytest.raises(InvalidArgumentError, match="set takes each key once"):
        check_ops(ContactOps(user_defined=ops))


def test_urls_relations_user_defined() -> None:
    p = person(
        urls=[item(True, value="https://ada.dev")],
        relations=[item(False, person="Byron", type="father")],
        userDefined=[item(False, key="Locker", value="42")],
    )
    ops = ContactOps(
        urls=ListOps(add=[Url(value="https://blog.ada.dev", type="blog")]),
        relations=ListOps(remove=["byron"]),
        user_defined=CustomFieldOps(remove=["LOCKER"], add=[UserDefined(key="Locker", value="43")]),
    )
    plan = plan_update(p, ops)
    assert plan.update_fields == ["urls", "relations", "userDefined"]
    assert plan.fields["urls"][1] == {"value": "https://blog.ada.dev", "type": "blog"}
    assert plan.fields["relations"] == []
    assert plan.fields["userDefined"] == [{"key": "Locker", "value": "43"}]
    ops2 = ContactOps(relations=ListOps(add=[Relation(person="Mary", type="mother")]))
    assert plan_update(p, ops2).fields["relations"][1] == {"person": "Mary", "type": "mother"}


def test_events_match_by_date() -> None:
    p = person(
        events=[
            item(False, date={"year": 1835, "month": 7, "day": 8}, type="anniversary"),
            item(False, date={"month": 12, "day": 10}, type="other"),
        ]
    )
    plan = plan_update(p, ContactOps(events=ListOps(remove=["1835-07-08", "--12-10"])))
    assert plan.fields["events"] == []
    ops = ContactOps(events=ListOps(add=[Event(date=PartialDate(month=3, day=1), type="x")]))
    assert plan_update(p, ops).fields["events"][2] == {
        "date": {"month": 3, "day": 1},
        "type": "x",
    }


def test_nicknames() -> None:
    p = person(nicknames=[item(False, value="Ada")])
    plan = plan_update(p, ContactOps(nicknames=ListOps(add=["Enchantress"], remove=["ada"])))
    assert plan.fields["nicknames"] == [{"value": "Enchantress"}]
    assert plan.changes[0].before == ["Ada"]
    assert plan.changes[0].after == ["Enchantress"]


# --- single-valued fields --------------------------------------------------------------


def test_name_merge_keeps_the_rest_of_the_item() -> None:
    p = ada()
    p["names"][0]["phoneticGivenName"] = "AY-da"
    plan = plan_update(p, ContactOps(name=NameFields(family="King")))
    assert plan.update_fields == ["names"]
    new = plan.fields["names"][0]
    assert new["givenName"] == "Ada"
    assert new["familyName"] == "King"
    assert new["phoneticGivenName"] == "AY-da"
    assert new["metadata"] == p["names"][0]["metadata"]
    # Derived forms are left for Google to recompute.
    assert "displayName" not in new
    assert "unstructuredName" not in new
    change = plan.changes[0]
    assert change.field == "name"
    assert change.before == {"given": "Ada", "family": "Lovelace", "display": "Ada Lovelace"}
    assert change.after == {"given": "Ada", "family": "King"}


def test_name_empty_part_clears_it_and_same_value_is_noop() -> None:
    plan = plan_update(ada(), ContactOps(name=NameFields(given="Ada")))
    assert plan.update_fields == []
    plan = plan_update(ada(), ContactOps(name=NameFields(family="")))
    assert "familyName" not in plan.fields["names"][0]


def test_name_edits_the_contact_source_not_the_profile() -> None:
    p = ada()
    profile = {
        "metadata": {"primary": True, "source": {"type": "PROFILE", "id": "1"}},
        "givenName": "Augusta",
        "displayName": "Augusta Ada",
    }
    p["names"][0]["metadata"]["primary"] = False
    p["names"].insert(0, profile)
    new = plan_update(p, ContactOps(name=NameFields(given="Ada B.")))
    assert new.fields["names"][0] == profile
    assert new.fields["names"][1]["givenName"] == "Ada B."


def test_a_name_part_cleared_on_a_contact_without_a_name_writes_nothing() -> None:
    p = ada()
    del p["names"]
    assert plan_update(p, ContactOps(name=NameFields(given=""))).update_fields == []


PROFILE = {"type": "PROFILE", "id": "1"}


def with_profile_email(value: str) -> dict[str, Any]:
    p = ada()
    p["emailAddresses"].append({"value": value, "metadata": {"source": PROFILE}})
    return p


def test_values_from_the_profile_cannot_be_removed_or_made_primary() -> None:
    p = with_profile_email("ada@profile.example")
    with pytest.raises(InvalidArgumentError, match="Google profile") as err:
        plan_update(p, ContactOps(emails=ListOps(remove=["ada@profile.example"])))
    assert "removed" in str(err.value)
    with pytest.raises(InvalidArgumentError, match="Google profile") as err:
        plan_update(p, ContactOps(emails=ListOps(set_primary="ada@profile.example")))
    assert "made primary" in str(err.value)


def test_remove_drops_only_the_contacts_own_copy_of_a_value() -> None:
    p = with_profile_email("ada@example.com")
    plan = plan_update(p, ContactOps(emails=ListOps(remove=["ada@example.com"])))
    assert [e["value"] for e in plan.fields["emailAddresses"]] == [
        "Countess@Example.org",
        "ada@example.com",
    ]
    assert plan.fields["emailAddresses"][1]["metadata"]["source"] == PROFILE


def test_add_of_a_value_only_the_profile_has_gives_the_contact_its_own() -> None:
    p = with_profile_email("ada@profile.example")
    plan = plan_update(p, ContactOps(emails=ListOps(add=[Email(value="ada@profile.example")])))
    assert plan.fields["emailAddresses"][-1] == {"value": "ada@profile.example"}


def test_birthday_and_biography_replace() -> None:
    p = ada(
        birthdays=[item(True, date={"year": 1815, "month": 12, "day": 10}, text="10/12/1815")],
        biographies=[item(True, value="old notes", contentType="TEXT_PLAIN")],
    )
    ops = ContactOps(birthday=PartialDate(month=12, day=10), biography="new notes")
    plan = plan_update(p, ops)
    assert plan.update_fields == ["birthdays", "biographies"]
    bday = plan.fields["birthdays"][0]
    assert bday["date"] == {"month": 12, "day": 10}
    assert "text" not in bday
    assert bday["metadata"] == p["birthdays"][0]["metadata"]
    assert plan.fields["biographies"][0]["value"] == "new notes"
    changes = {c.field: c for c in plan.changes}
    assert changes["birthday"].before == {"year": 1815, "month": 12, "day": 10}
    assert changes["birthday"].after == {"month": 12, "day": 10}
    assert changes["biography"].before == "old notes"


def test_clear() -> None:
    p = ada(biographies=[item(True, value="notes")])
    plan = plan_update(p, ContactOps(clear=["biography", "birthday"]))
    # No birthday to clear: only biographies changes.
    assert plan.update_fields == ["biographies"]
    assert plan.fields["biographies"] == []


def test_clear_and_set_refused() -> None:
    with pytest.raises(InvalidArgumentError, match="Both set and cleared"):
        plan_update(ada(), ContactOps(biography="x", clear=["biography"]))


def test_nothing_to_do_refused() -> None:
    with pytest.raises(InvalidArgumentError, match="Nothing to update"):
        plan_update(ada(), ContactOps())


def test_update_fields_only_touched_and_body_minimal() -> None:
    p = ada()
    plan = plan_update(
        p,
        ContactOps(
            phones=ListOps(add=[Phone(value="123")]),
            emails=ListOps(add=[Email(value="ada@example.com")]),  # already there: no-op
        ),
    )
    assert plan.update_fields == ["phoneNumbers"]
    body = plan.body(p)
    assert set(body) == {"resourceName", "etag", "metadata", "phoneNumbers"}
    assert body["etag"] == "etag-1"
    full = plan.body(p, ["emailAddresses", "phoneNumbers"])
    assert full["emailAddresses"] == p["emailAddresses"]


# --- create ----------------------------------------------------------------------------


def test_build_person() -> None:
    body = build_person(
        ContactFields(
            name=NameFields(given=" Ada ", family="Lovelace"),
            emails=[Email(value="a@x.io"), Email(value="b@x.io", type="work", primary=True)],
            phones=[Phone(value="+39 333", canonical="ignored")],
            addresses=[Address(formatted="Via Roma 1, Milano", country_code="IT")],
            birthday=PartialDate(month=12, day=10),
            biography="notes",
        ),
        ["contactGroups/myContacts", "contactGroups/abc"],
    )
    assert body["names"] == [{"givenName": "Ada", "familyName": "Lovelace"}]
    assert body["emailAddresses"] == [
        {"value": "b@x.io", "type": "work", "metadata": {"primary": True, "sourcePrimary": True}},
        {"value": "a@x.io"},
    ]
    assert body["phoneNumbers"] == [{"value": "+39 333"}]
    assert body["addresses"] == [{"formattedValue": "Via Roma 1, Milano", "countryCode": "IT"}]
    assert body["birthdays"] == [{"date": {"month": 12, "day": 10}}]
    assert body["biographies"] == [{"value": "notes", "contentType": "TEXT_PLAIN"}]
    assert body["memberships"] == [
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}},
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc"}},
    ]


def test_build_person_refuses_empty() -> None:
    with pytest.raises(InvalidArgumentError, match="at least one field"):
        build_person(ContactFields(groups=["x"]))
