"""Matching contacts in ``find_contacts``. The People API cannot filter a listing, so this
runs over raw person objects fetched with ``LISTING_FIELDS``. Pure, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from contacts_mcp.fields import SYSTEM_GROUPS
from contacts_mcp.models import MissingField
from contacts_mcp.normalize import (
    folded,
    group_resource_names,
    has_notes,
    time_from_api,
    update_time,
)

JSON = dict[str, Any]

#: ``missing`` value → the People API field that has to be empty.
_MISSING_API_FIELD: dict[MissingField, str] = {
    "email": "emailAddresses",
    "phone": "phoneNumbers",
    "organization": "organizations",
    "address": "addresses",
    "birthday": "birthdays",
}


def _lacks(person: JSON, field: MissingField) -> bool:
    if field == "notes":
        return not has_notes(person)
    if field == "groups":
        return all(rn in SYSTEM_GROUPS for rn in group_resource_names(person))
    return not person.get(_MISSING_API_FIELD[field])


def _custom_keys(person: JSON) -> set[str]:
    items = person.get("userDefined")
    if not isinstance(items, list):
        return set()
    return {folded(i.get("key")) for i in items if isinstance(i, dict)}


def _updated_before(person: JSON, cutoff: date) -> bool:
    value = update_time(person)
    when = time_from_api(value) if value else None
    return when is not None and when < datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC)


@dataclass(frozen=True)
class ContactFilter:
    """Criteria that must all hold. Custom field keys match trimmed and case-insensitively."""

    missing: tuple[MissingField, ...] = ()
    custom_fields_missing: tuple[str, ...] = ()
    custom_fields_present: tuple[str, ...] = ()
    updated_before: date | None = None

    def __bool__(self) -> bool:
        """Whether there is any criterion."""
        return bool(
            self.missing
            or self.custom_fields_missing
            or self.custom_fields_present
            or self.updated_before
        )

    def matches(self, person: JSON) -> bool:
        keys = _custom_keys(person)
        return (
            all(_lacks(person, field) for field in self.missing)
            and all(folded(key) not in keys for key in self.custom_fields_missing)
            and all(folded(key) in keys for key in self.custom_fields_present)
            and (self.updated_before is None or _updated_before(person, self.updated_before))
        )
