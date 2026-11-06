"""People API JSON → tool models (read direction). Every string goes through
:func:`contacts_mcp.sanitize.clean`; ``metadata`` is dropped, except that values from the
person's Google profile (not the contact itself) are listed as ``from_profile``, and the
contact source's ``updateTime`` is shown as ``updated_at``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from contacts_mcp.models import (
    Address,
    Contact,
    ContactSummary,
    Email,
    Event,
    Group,
    GroupRef,
    Name,
    Organization,
    OtherContact,
    PartialDate,
    Phone,
    Relation,
    Url,
    UserDefined,
)
from contacts_mcp.sanitize import clean, clean_json

JSON = dict[str, Any]

MY_CONTACTS = "contactGroups/myContacts"


def _s(value: Any) -> str | None:
    """A cleaned, non-empty string, or None."""
    if not isinstance(value, str):
        return None
    value = clean(value).strip()
    return value or None


def folded(value: Any) -> str:
    """Trimmed, whitespace-collapsed, case-folded text ('' for a non-string)."""
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def _id(value: Any) -> str:
    """A Google identifier (resource name, etag), cleaned like any other returned string."""
    return clean(value) if isinstance(value, str) else ""


def _items(person: JSON, field: str) -> list[JSON]:
    items = person.get(field)
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def is_primary(item: JSON) -> bool:
    meta = item.get("metadata")
    return isinstance(meta, dict) and meta.get("primary") is True


def is_contact_data(item: JSON) -> bool:
    """Whether a field value belongs to the contact itself. Values from the person's own
    Google profile (``metadata.source.type`` ``PROFILE``, ``DOMAIN_PROFILE``, …) are shown
    with the contact, but Google ignores them on update."""
    meta = item.get("metadata")
    source = meta.get("source") if isinstance(meta, dict) else None
    kind = source.get("type") if isinstance(source, dict) else None
    return kind in (None, "CONTACT")


def primary_first(items: list[JSON]) -> list[JSON]:
    """Items with the primary one first (stable otherwise)."""
    return sorted(items, key=lambda i: not is_primary(i))


def _primary(items: list[JSON]) -> JSON | None:
    ordered = primary_first(items)
    return ordered[0] if ordered else None


def _type(item: JSON) -> str | None:
    return _s(item.get("type")) or _s(item.get("formattedType"))


def date_from_api(value: Any) -> PartialDate | None:
    if not isinstance(value, dict):
        return None
    year, month, day = (value.get(k) or None for k in ("year", "month", "day"))
    try:
        return PartialDate(year=year, month=month, day=day)
    except ValueError:
        return None


def name_from_api(item: JSON) -> Name:
    return Name(
        given=_s(item.get("givenName")),
        family=_s(item.get("familyName")),
        middle=_s(item.get("middleName")),
        prefix=_s(item.get("honorificPrefix")),
        suffix=_s(item.get("honorificSuffix")),
        display=_s(item.get("displayName")),
    )


def email_from_api(item: JSON) -> Email | None:
    value = _s(item.get("value"))
    if value is None or len(value) < 3:
        return None
    return Email(value=value, type=_type(item), primary=is_primary(item))


def phone_from_api(item: JSON) -> Phone | None:
    value = _s(item.get("value"))
    if value is None:
        return None
    return Phone(
        value=value,
        type=_type(item),
        primary=is_primary(item),
        canonical=_s(item.get("canonicalForm")),
    )


def organization_from_api(item: JSON) -> Organization:
    current = item.get("current")
    return Organization(
        name=_s(item.get("name")),
        title=_s(item.get("title")),
        department=_s(item.get("department")),
        type=_type(item),
        current=current if isinstance(current, bool) else None,
        primary=is_primary(item),
    )


def address_from_api(item: JSON) -> Address:
    return Address(
        formatted=_s(item.get("formattedValue")),
        street=_s(item.get("streetAddress")),
        extended=_s(item.get("extendedAddress")),
        po_box=_s(item.get("poBox")),
        city=_s(item.get("city")),
        region=_s(item.get("region")),
        postal_code=_s(item.get("postalCode")),
        country=_s(item.get("country")),
        country_code=_s(item.get("countryCode")),
        type=_type(item),
        primary=is_primary(item),
    )


def url_from_api(item: JSON) -> Url | None:
    value = _s(item.get("value"))
    if value is None:
        return None
    return Url(value=value, type=_type(item), primary=is_primary(item))


def relation_from_api(item: JSON) -> Relation | None:
    person = _s(item.get("person"))
    if person is None:
        return None
    return Relation(person=person, type=_type(item))


def event_from_api(item: JSON) -> Event | None:
    date = date_from_api(item.get("date"))
    if date is None:
        return None
    return Event(date=date, type=_type(item))


def user_defined_from_api(item: JSON) -> UserDefined | None:
    key = _s(item.get("key"))
    if key is None:
        return None
    return UserDefined(key=key, value=_s(item.get("value")) or "")


def group_resource_names(person: JSON) -> list[str]:
    names: list[str] = []
    for m in _items(person, "memberships"):
        cgm = m.get("contactGroupMembership")
        if isinstance(cgm, dict):
            rn = cgm.get("contactGroupResourceName")
            if isinstance(rn, str):
                names.append(rn)
    return names


def _group_name(rn: str, group_names: dict[str, str]) -> str:
    if rn in group_names:
        return clean(group_names[rn])
    return clean(rn.removeprefix("contactGroups/"))


def display_name(person: JSON) -> str | None:
    name = _primary(_items(person, "names"))
    return _s(name.get("displayName")) if name else None


def _summary_org(person: JSON) -> str | None:
    org = _primary(_items(person, "organizations"))
    if org is None:
        return None
    parts = [p for p in (_s(org.get("title")), _s(org.get("name"))) if p]
    return ", ".join(parts) or None


def time_from_api(value: str) -> datetime | None:
    """A Google RFC 3339 timestamp, always timezone-aware, or None if it isn't one."""
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def update_time(person: JSON) -> str | None:
    """When the contact itself last changed: the latest ``updateTime`` among its ``CONTACT``
    sources (the person's Google profile, also a source, changes on its own)."""
    meta = person.get("metadata")
    sources = meta.get("sources") if isinstance(meta, dict) else None
    if not isinstance(sources, list):
        return None
    latest: tuple[datetime, str] | None = None
    for source in sources:
        if not isinstance(source, dict) or source.get("type") != "CONTACT":
            continue
        value = _s(source.get("updateTime"))
        when = time_from_api(value) if value else None
        if value and when and (latest is None or when > latest[0]):
            latest = (when, value)
    return latest[1] if latest else None


#: Fields where each item is one value.
_COUNTED_LISTS = (
    "nicknames",
    "emailAddresses",
    "phoneNumbers",
    "organizations",
    "addresses",
    "urls",
    "relations",
    "events",
    "userDefined",
)


def _own_items(person: JSON, field: str) -> list[JSON]:
    return [item for item in _items(person, field) if is_contact_data(item)]


def has_notes(person: JSON) -> bool:
    """Whether the contact has notes of its own: a bio from the person's Google profile is
    what they say about themselves, not a note about them."""
    return any(_s(item.get("value")) for item in _own_items(person, "biographies"))


def field_count(person: JSON) -> int:
    """How many values the contact itself holds besides the name, groups and photo: not
    those from the person's Google profile. Notes and the birthday are one value each."""
    values = sum(len(_own_items(person, field)) for field in _COUNTED_LISTS)
    return values + has_notes(person) + bool(_own_items(person, "birthdays"))


def person_to_summary(
    person: JSON,
    *,
    group_names: dict[str, str],
    account: str | None = None,
    details: bool = False,
) -> ContactSummary:
    """``details`` fills the fields that need ``LISTING_FIELDS``: without it they stay None
    rather than claim, say, that a contact fetched without its notes has none."""
    email = _primary(_items(person, "emailAddresses"))
    phone = _primary(_items(person, "phoneNumbers"))
    return ContactSummary(
        resource_name=_id(person.get("resourceName")),
        account=account,
        display_name=display_name(person),
        email=_s(email.get("value")) if email else None,
        phone=_s(phone.get("value")) if phone else None,
        organization=_summary_org(person),
        # Every contact is in myContacts: listing it is noise.
        groups=[
            _group_name(rn, group_names) for rn in group_resource_names(person) if rn != MY_CONTACTS
        ],
        updated_at=update_time(person) if details else None,
        has_notes=has_notes(person) if details else None,
        field_count=field_count(person) if details else None,
    )


def _all[T](items: list[JSON], convert: Callable[[JSON], T | None]) -> list[T]:
    out: list[T] = []
    for item in primary_first(items):
        model = convert(item)
        if model is not None:
            out.append(model)
    return out


def person_to_contact(
    person: JSON, *, group_names: dict[str, str], account: str | None = None, raw: bool = False
) -> Contact:
    name = _primary(_items(person, "names"))
    birthday: PartialDate | None = None
    birthday_text: str | None = None
    for b in primary_first(_items(person, "birthdays")):
        birthday = date_from_api(b.get("date"))
        if birthday is not None:
            break
        birthday_text = birthday_text or _s(b.get("text"))
    bio = _primary(_items(person, "biographies"))
    photo = next((p for p in primary_first(_items(person, "photos")) if not p.get("default")), None)
    return Contact(
        resource_name=_id(person.get("resourceName")),
        etag=_id(person.get("etag")) or None,
        account=account,
        updated_at=update_time(person),
        name=name_from_api(name) if name else None,
        nicknames=[
            n for n in (_s(i.get("value")) for i in primary_first(_items(person, "nicknames"))) if n
        ],
        emails=_all(_items(person, "emailAddresses"), email_from_api),
        phones=_all(_items(person, "phoneNumbers"), phone_from_api),
        organizations=_all(_items(person, "organizations"), organization_from_api),
        addresses=_all(_items(person, "addresses"), address_from_api),
        birthday=birthday,
        birthday_text=None if birthday else birthday_text,
        biography=_s(bio.get("value")) if bio else None,
        urls=_all(_items(person, "urls"), url_from_api),
        relations=_all(_items(person, "relations"), relation_from_api),
        events=_all(_items(person, "events"), event_from_api),
        user_defined=_all(_items(person, "userDefined"), user_defined_from_api),
        groups=[
            GroupRef(resource_name=clean(rn), name=_group_name(rn, group_names))
            for rn in group_resource_names(person)
        ],
        photo_url=_s(photo.get("url")) if photo else None,
        from_profile=profile_values(person),
        raw=clean_json(person) if raw else None,
    )


# Field → (label, the key that holds the value shown for it).
_PROFILE_FIELDS = {
    "names": ("name", "displayName"),
    "nicknames": ("nickname", "value"),
    "emailAddresses": ("email", "value"),
    "phoneNumbers": ("phone", "value"),
    "organizations": ("organization", "name"),
    "addresses": ("address", "formattedValue"),
    "urls": ("url", "value"),
    "relations": ("relation", "person"),
    "biographies": ("biography", "value"),
}


def profile_values(person: JSON) -> list[str]:
    """``"email: ada@example.com"`` for each shown value from the person's Google profile."""
    out: list[str] = []
    for field, (label, key) in _PROFILE_FIELDS.items():
        for item in _items(person, field):
            value = _s(item.get(key))
            if value and not is_contact_data(item):
                out.append(f"{label}: {value}")
    return out


def other_to_model(person: JSON, *, account: str | None = None) -> OtherContact:
    return OtherContact(
        resource_name=_id(person.get("resourceName")),
        account=account,
        display_name=display_name(person),
        emails=[
            v
            for v in (_s(i.get("value")) for i in primary_first(_items(person, "emailAddresses")))
            if v
        ],
        phones=[
            v
            for v in (_s(i.get("value")) for i in primary_first(_items(person, "phoneNumbers")))
            if v
        ],
    )


def group_to_model(group: JSON) -> Group:
    rn = _id(group.get("resourceName"))
    count = group.get("memberCount")
    return Group(
        resource_name=rn,
        name=_s(group.get("formattedName")) or _s(group.get("name")) or rn,
        type="system" if group.get("groupType") == "SYSTEM_CONTACT_GROUP" else "user",
        member_count=count if isinstance(count, int) else None,
    )
