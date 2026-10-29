"""Normalization of People API persons into the tool models."""

from __future__ import annotations

from typing import Any

from contacts_mcp.models import PartialDate
from contacts_mcp.normalize import (
    group_to_model,
    other_to_model,
    person_to_contact,
    person_to_summary,
)
from tests.conftest import person

GROUPS = {
    "contactGroups/myContacts": "My Contacts",
    "contactGroups/f": "Family",
    "contactGroups/w": "Work",
}


def item(value: str, *, primary: bool = False, **extra: Any) -> dict[str, Any]:
    return {"metadata": {"primary": primary}, "value": value, **extra}


# --- summaries --------------------------------------------------------------------------


def test_summary_of_a_typical_contact() -> None:
    p = person(
        emails=["ada@example.com"],
        phones=["+39 333 1234567"],
        groups=["contactGroups/myContacts", "contactGroups/f"],
        organizations=[{"name": "Analytical Engines", "title": "Programmer"}],
    )
    summary = person_to_summary(p, group_names=GROUPS)
    assert summary.model_dump() == {
        "resource_name": "people/c1",
        "account": None,
        "display_name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "+39 333 1234567",
        "organization": "Programmer, Analytical Engines",
        "groups": ["Family"],
    }


def test_summary_takes_the_primary_value_not_the_first() -> None:
    p = person(
        emailAddresses=[
            item("old@example.com"),
            item("main@example.com", primary=True),
        ],
        phoneNumbers=[item("111"), item("222", primary=True)],
        organizations=[
            {"name": "Old Co", "title": "Intern"},
            {"name": "New Co", "metadata": {"primary": True}},
        ],
    )
    summary = person_to_summary(p, group_names={})
    assert summary.email == "main@example.com"
    assert summary.phone == "222"
    assert summary.organization == "New Co"


def test_summary_of_an_empty_person_has_only_the_resource_name() -> None:
    summary = person_to_summary({"resourceName": "people/c9"}, group_names={})
    assert summary.resource_name == "people/c9"
    assert summary.display_name is None
    assert summary.email is None
    assert summary.phone is None
    assert summary.organization is None
    assert summary.groups == []


def test_summary_survives_malformed_items() -> None:
    p = {
        "resourceName": "people/c9",
        "emailAddresses": ["nope", {"value": 12}, {"value": ""}],
        "names": "nope",
        "memberships": [{"contactGroupMembership": "x"}, {}],
    }
    summary = person_to_summary(p, group_names={})
    assert summary.email is None
    assert summary.display_name is None
    assert summary.groups == []


def test_summary_groups_use_the_name_map_and_omit_my_contacts() -> None:
    p = person(
        groups=[
            "contactGroups/myContacts",
            "contactGroups/f",
            "contactGroups/w",
            "contactGroups/unknown1",
        ]
    )
    summary = person_to_summary(p, group_names=GROUPS)
    assert summary.groups == ["Family", "Work", "unknown1"]  # unknown ids fall back to the id


def test_summary_account_tag() -> None:
    assert person_to_summary(person(), group_names={}, account="work").account == "work"


def test_summary_strings_are_sanitized() -> None:
    p = person(emails=["a\u200bda@example.com"], phones=["+39\u202e 333"])
    p["names"][0]["displayName"] = "Ad\u200ba Love\u202elace"
    p["organizations"] = [{"name": "Eng\u2066ines", "title": "Pro\x00grammer"}]
    p["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/f"}}]
    summary = person_to_summary(p, group_names={"contactGroups/f": "Fam\u200bily"})
    assert summary.display_name == "Ada Lovelace"
    assert summary.email == "ada@example.com"
    assert summary.phone == "+39 333"
    assert summary.organization == "Programmer, Engines"
    assert summary.groups == ["Family"]


# --- full contacts ----------------------------------------------------------------------


def test_contact_orders_multi_valued_fields_primary_first() -> None:
    p = person(
        emailAddresses=[
            item("a@example.com", type="work"),
            item("b@example.com", primary=True, type="home"),
            item("c@example.com"),
        ],
        phoneNumbers=[item("1"), item("2", primary=True)],
    )
    contact = person_to_contact(p, group_names={})
    assert [e.value for e in contact.emails] == ["b@example.com", "a@example.com", "c@example.com"]
    assert [e.primary for e in contact.emails] == [True, False, False]
    assert contact.emails[0].type == "home"
    assert [ph.value for ph in contact.phones] == ["2", "1"]


def test_contact_full_record() -> None:
    p = person(
        etag="etag-9",
        nicknames=[{"value": "Countess"}],
        organizations=[
            {
                "name": "Analytical Engines",
                "title": "Programmer",
                "department": "R&D",
                "type": "work",
                "current": True,
            }
        ],
        addresses=[
            {
                "formattedValue": "1 Main St, London",
                "streetAddress": "1 Main St",
                "city": "London",
                "countryCode": "GB",
                "formattedType": "Home",
            }
        ],
        birthdays=[{"date": {"month": 12, "day": 10}}],
        biographies=[{"value": "Wrote the first program."}],
        urls=[{"value": "https://example.com", "type": "homePage"}],
        relations=[{"person": "Charles", "type": "friend"}],
        events=[{"date": {"year": 1815, "month": 12, "day": 10}, "type": "anniversary"}],
        userDefined=[{"key": "Pet", "value": "Cat"}],
        photos=[{"url": "https://photos.example.com/a", "default": False}],
        phoneNumbers=[
            {"value": "+39 333 1234567", "canonicalForm": "+393331234567", "type": "mobile"}
        ],
        groups=["contactGroups/myContacts", "contactGroups/f"],
    )
    c = person_to_contact(p, group_names=GROUPS)
    assert c.resource_name == "people/c1"
    assert c.etag == "etag-9"
    assert c.name is not None
    assert (c.name.given, c.name.family, c.name.display) == ("Ada", "Lovelace", "Ada Lovelace")
    assert c.nicknames == ["Countess"]
    assert c.organizations[0].department == "R&D"
    assert c.organizations[0].current is True
    assert c.addresses[0].formatted == "1 Main St, London"
    assert c.addresses[0].type == "Home"
    assert c.addresses[0].country_code == "GB"
    assert c.birthday == PartialDate(month=12, day=10)
    assert c.biography == "Wrote the first program."
    assert c.urls[0].type == "homePage"
    assert (c.relations[0].person, c.relations[0].type) == ("Charles", "friend")
    assert c.events[0].date == PartialDate(year=1815, month=12, day=10)
    assert (c.user_defined[0].key, c.user_defined[0].value) == ("Pet", "Cat")
    assert c.phones[0].canonical == "+393331234567"
    assert c.photo_url == "https://photos.example.com/a"
    # Unlike summaries, the full record lists every group, myContacts included.
    assert [(g.resource_name, g.name) for g in c.groups] == [
        ("contactGroups/myContacts", "My Contacts"),
        ("contactGroups/f", "Family"),
    ]
    assert c.raw is None


def test_a_birthday_held_only_as_text_is_shown_as_text() -> None:
    c = person_to_contact(person(birthdays=[{"text": "early May"}]), group_names={})
    assert (c.birthday, c.birthday_text) == (None, "early May")
    dated = person(birthdays=[{"text": "early May"}, {"date": {"year": 1815}}])
    assert person_to_contact(dated, group_names={}).birthday == PartialDate(year=1815)
    assert person_to_contact(dated, group_names={}).birthday_text is None


def test_type_wins_over_formatted_type() -> None:
    p = person(emailAddresses=[item("a@example.com", type="work", formattedType="Home")])
    assert person_to_contact(p, group_names={}).emails[0].type == "work"


def test_values_from_the_google_profile_are_listed_as_read_only() -> None:
    profile = {"source": {"type": "PROFILE", "id": "1"}}
    p = person(
        emailAddresses=[
            item("ada@example.com"),
            {"value": "ada@profile.example", "metadata": profile},
        ],
        names=[{"displayName": "Ada L.", "metadata": {"source": {"type": "DOMAIN_PROFILE"}}}],
    )
    c = person_to_contact(p, group_names={})
    assert c.from_profile == ["name: Ada L.", "email: ada@profile.example"]
    assert [e.value for e in c.emails] == ["ada@example.com", "ada@profile.example"]


def test_nicknames_put_the_primary_first_and_blank_notes_are_none() -> None:
    p = person(
        nicknames=[item("Ada"), item("Countess", primary=True)],
        biographies=[{"value": " \u200b "}],
    )
    c = person_to_contact(p, group_names={})
    assert c.nicknames == ["Countess", "Ada"]
    assert c.biography is None


def test_identifiers_are_sanitized_too() -> None:
    p = person("people/c\u200b1", etag="e\u202e1", groups=["contactGroups/x\u200by"])
    c = person_to_contact(p, group_names={})
    assert (c.resource_name, c.etag) == ("people/c1", "e1")
    assert [(g.resource_name, g.name) for g in c.groups] == [("contactGroups/xy", "xy")]


def test_contact_of_an_empty_person() -> None:
    c = person_to_contact({"resourceName": "people/c9"}, group_names={})
    assert c.resource_name == "people/c9"
    assert c.etag is None
    assert c.name is None
    assert c.birthday is None
    assert c.biography is None
    assert c.photo_url is None
    for items in (c.emails, c.phones, c.organizations, c.addresses):
        assert items == []
    for more in (c.urls, c.relations, c.events, c.user_defined, c.groups):
        assert more == []


def test_default_photo_is_skipped() -> None:
    only_default = person(photos=[{"url": "https://photos.example.com/d", "default": True}])
    assert person_to_contact(only_default, group_names={}).photo_url is None
    mixed = person(
        photos=[
            {"url": "https://photos.example.com/d", "default": True},
            {"url": "https://photos.example.com/real"},
        ]
    )
    assert person_to_contact(mixed, group_names={}).photo_url == "https://photos.example.com/real"


def test_invalid_items_are_dropped_not_fatal() -> None:
    p = person(
        emailAddresses=[{"value": "x"}, item("ok@example.com")],  # too short for an Email
        urls=[{"type": "home"}],
        relations=[{"type": "friend"}],
        events=[{"date": {"day": 3}}],  # a day alone is not a date
        userDefined=[{"value": "orphan"}],
        birthdays=[{"date": {}}],
    )
    c = person_to_contact(p, group_names={})
    assert [e.value for e in c.emails] == ["ok@example.com"]
    for items in (c.urls, c.relations, c.events, c.user_defined):
        assert items == []
    assert c.birthday is None


def test_contact_strings_are_sanitized() -> None:
    p = person(
        biographies=[{"value": "hello\u200b \u202eworld\x00"}],
        nicknames=[{"value": "Ni\u200bck"}],
    )
    c = person_to_contact(p, group_names={})
    assert c.biography == "hello world"
    assert c.nicknames == ["Nick"]


def test_raw_is_included_only_on_request_and_sanitized() -> None:
    p = person(biographies=[{"value": "a\u200bb", "metadata": {"source": {"type": "CONTACT"}}}])
    assert person_to_contact(p, group_names={}).raw is None
    raw = person_to_contact(p, group_names={}, raw=True).raw
    assert raw is not None
    assert raw["resourceName"] == "people/c1"
    assert raw["biographies"][0]["value"] == "ab"  # type: ignore[index, call-overload]
    assert "metadata" in raw  # unlike the normalized form, raw keeps Google's metadata


# --- other contacts and groups ----------------------------------------------------------


def test_other_contact() -> None:
    p = {
        "resourceName": "otherContacts/c5",
        "names": [{"displayName": "Mal\u200blory", "metadata": {"primary": True}}],
        "emailAddresses": [item("z@example.com"), item("y@example.com", primary=True)],
        "phoneNumbers": [item("123")],
    }
    o = other_to_model(p, account="work")
    assert o.resource_name == "otherContacts/c5"
    assert o.account == "work"
    assert o.display_name == "Mallory"
    assert o.emails == ["y@example.com", "z@example.com"]
    assert o.phones == ["123"]


def test_other_contact_with_nothing() -> None:
    o = other_to_model({"resourceName": "otherContacts/c5"})
    assert o.display_name is None
    assert o.emails == []
    assert o.phones == []


def test_group_to_model() -> None:
    user = group_to_model(
        {
            "resourceName": "contactGroups/f",
            "name": "Family",
            "groupType": "USER_CONTACT_GROUP",
            "memberCount": 3,
        }
    )
    assert (user.name, user.type, user.member_count) == ("Family", "user", 3)
    system = group_to_model(
        {
            "resourceName": "contactGroups/myContacts",
            "name": "myContacts",
            "formattedName": "My Contacts",
            "groupType": "SYSTEM_CONTACT_GROUP",
        }
    )
    assert (system.name, system.type, system.member_count) == ("My Contacts", "system", None)
