"""Matching of raw persons against the find_contacts criteria."""

from __future__ import annotations

from datetime import date
from typing import Any, get_args

import pytest

from contacts_mcp.contact_filter import ContactFilter
from contacts_mcp.models import MissingField
from tests.conftest import person

FULL: dict[str, Any] = {
    "emailAddresses": [{"value": "a@x.io"}],
    "phoneNumbers": [{"value": "1"}],
    "organizations": [{"name": "ACME"}],
    "addresses": [{"formattedValue": "1 Main St"}],
    "birthdays": [{"date": {"month": 1}}],
    "biographies": [{"value": "Notes"}],
}


def full(**extra: Any) -> dict[str, Any]:
    return person(groups=["contactGroups/myContacts", "contactGroups/f"], **{**FULL, **extra})


def timed(update_time: str) -> dict[str, Any]:
    return person(metadata={"sources": [{"type": "CONTACT", "updateTime": update_time}]})


@pytest.mark.parametrize("field", get_args(MissingField))
def test_missing_matches_only_a_contact_without_it(field: MissingField) -> None:
    criteria = ContactFilter(missing=(field,))
    assert criteria.matches(person())
    assert not criteria.matches(full())


def test_only_my_contacts_counts_as_no_groups() -> None:
    assert ContactFilter(missing=("groups",)).matches(person(groups=["contactGroups/myContacts"]))


def test_starred_is_not_a_label() -> None:
    p = person(groups=["contactGroups/myContacts", "contactGroups/starred"])
    assert ContactFilter(missing=("groups",)).matches(p)


def test_a_starred_contact_with_a_label_has_one() -> None:
    p = person(groups=["contactGroups/starred", "contactGroups/f"])
    assert not ContactFilter(missing=("groups",)).matches(p)


def test_a_profile_bio_does_not_stop_notes_being_missing() -> None:
    bio = {"value": "About me", "metadata": {"source": {"type": "PROFILE"}}}
    assert ContactFilter(missing=("notes",)).matches(person(biographies=[bio]))


def test_blank_notes_are_missing() -> None:
    assert ContactFilter(missing=("notes",)).matches(full(biographies=[{"value": " "}]))


def test_custom_fields_match_keys_case_insensitively() -> None:
    p = person(userDefined=[{"key": " Last_Reviewed ", "value": "x"}])
    assert ContactFilter(custom_fields_present=("last_reviewed",)).matches(p)
    assert not ContactFilter(custom_fields_missing=("LAST_REVIEWED",)).matches(p)
    assert ContactFilter(custom_fields_missing=("other",)).matches(p)
    assert not ContactFilter(custom_fields_present=("other",)).matches(p)


def test_a_contact_without_custom_fields_lacks_any_key() -> None:
    assert ContactFilter(custom_fields_missing=("k",)).matches(person())
    assert ContactFilter(custom_fields_missing=("k",)).matches(person(userDefined="nope"))


def test_updated_before_is_exclusive_at_midnight_utc() -> None:
    criteria = ContactFilter(updated_before=date(2024, 6, 1))
    assert criteria.matches(timed("2024-05-31T23:59:59.9Z"))
    assert not criteria.matches(timed("2024-06-01T00:00:00Z"))


def test_a_contact_with_no_known_time_is_never_updated_before() -> None:
    assert not ContactFilter(updated_before=date(2099, 1, 1)).matches(person())


def test_criteria_must_all_hold() -> None:
    criteria = ContactFilter(missing=("email", "phone"), custom_fields_missing=("k",))
    assert criteria.matches(person())
    assert not criteria.matches(person(phones=["1"]))
    assert not criteria.matches(person(userDefined=[{"key": "k", "value": "v"}]))


def test_truthiness_says_whether_there_are_criteria() -> None:
    assert not ContactFilter()
    assert ContactFilter(updated_before=date(2024, 1, 1))
    assert ContactFilter(custom_fields_present=("k",))
