"""The non-destructive update engine: pure functions over People API person JSON.

See docs/DESIGN.md, "Update engine". Nothing here does I/O: the write tools fetch the
person, call :func:`plan_update` (or :func:`build_person` for a new contact), send the
result, and retry on an etag conflict by planning again on the fresh person.

Existing items are kept **verbatim** (their ``metadata``, ``formattedType`` and any
sub-field this server does not know), except that an ``add`` of a value already there with
another type changes that item's type, and a ``set`` changes the value of the custom field
it names; only new items are built from the tool models.
Values from the person's own Google profile are never edited: Google ignores them.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field, JsonValue

from contacts_mcp.errors import InvalidArgumentError
from contacts_mcp.fields import WRITABLE_FIELDS
from contacts_mcp.models import (
    Address,
    ClearableField,
    CustomFieldOps,
    Email,
    Event,
    FieldChange,
    ListOps,
    Model,
    NameFields,
    Organization,
    PartialDate,
    PersonResourceName,
    Phone,
    Relation,
    Url,
    UserDefined,
    json_dump,
)
from contacts_mcp.normalize import (
    address_from_api,
    date_from_api,
    email_from_api,
    event_from_api,
    folded,
    is_contact_data,
    is_primary,
    name_from_api,
    organization_from_api,
    phone_from_api,
    primary_first,
    relation_from_api,
    url_from_api,
    user_defined_from_api,
)
from contacts_mcp.sanitize import clean

JSON = dict[str, Any]

#: Name parts (tool model attribute → People API key).
_NAME_PARTS = {
    "given": "givenName",
    "family": "familyName",
    "middle": "middleName",
    "prefix": "honorificPrefix",
    "suffix": "honorificSuffix",
}
#: Name keys Google derives from the parts: dropped when a part changes, so they are
#: recomputed instead of contradicting the new parts.
_DERIVED_NAME_KEYS = ("displayName", "displayNameLastFirst", "unstructuredName")


# --- tool input models (shared by the single and batch tools) --------------------------


class ContactFields(Model):
    """A new contact's fields (create_contact, batch_create_contacts)."""

    name: NameFields | None = None
    nicknames: list[str] = Field(default_factory=list)
    emails: list[Email] = Field(default_factory=list)
    phones: list[Phone] = Field(default_factory=list)
    organizations: list[Organization] = Field(default_factory=list)
    addresses: list[Address] = Field(default_factory=list)
    urls: list[Url] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    user_defined: list[UserDefined] = Field(default_factory=list, description="Custom fields.")
    birthday: PartialDate | None = None
    biography: str | None = Field(default=None, description="Notes.")
    groups: list[str] = Field(
        default_factory=list,
        description="Groups to put the contact in: resource names or exact names.",
    )


class ContactOps(Model):
    """Changes to an existing contact (update_contact, batch_update_contacts)."""

    name: NameFields | None = Field(
        default=None, description="Only the parts given change; '' clears a part."
    )
    nicknames: ListOps[str] | None = None
    emails: ListOps[Email] | None = None
    phones: ListOps[Phone] | None = None
    organizations: ListOps[Organization] | None = None
    addresses: ListOps[Address] | None = None
    urls: ListOps[Url] | None = None
    relations: ListOps[Relation] | None = None
    events: ListOps[Event] | None = None
    user_defined: CustomFieldOps | None = None
    birthday: PartialDate | None = Field(default=None, description="Replaces the birthday.")
    biography: str | None = Field(default=None, description="Replaces the notes ('' clears).")
    clear: list[ClearableField] = Field(
        default_factory=list, description="Single-valued fields to empty."
    )


class ContactUpdate(ContactOps):
    """One contact's changes in batch_update_contacts."""

    resource_name: PersonResourceName


# --- item builders (model → People API item) -------------------------------------------


def _put(item: JSON, key: str, value: Any) -> None:
    if isinstance(value, str):
        value = value.strip()
    if value is not None and value != "":
        item[key] = value


def date_to_api(date: PartialDate) -> JSON:
    out: JSON = {}
    for key in ("year", "month", "day"):
        value = getattr(date, key)
        if value is not None:
            out[key] = value
    return out


def name_to_api(name: NameFields) -> JSON:
    item: JSON = {}
    for attr, key in _NAME_PARTS.items():
        _put(item, key, getattr(name, attr))
    return item


def _typed(value_key: str, value: str, type_: str | None) -> JSON:
    item: JSON = {}
    _put(item, value_key, value)
    _put(item, "type", type_)
    return item


def email_to_api(email: Email) -> JSON:
    return _typed("value", email.value, email.type)


def phone_to_api(phone: Phone) -> JSON:
    # ``canonical`` is computed by Google (read only): not written.
    return _typed("value", phone.value, phone.type)


def url_to_api(url: Url) -> JSON:
    return _typed("value", url.value, url.type)


def relation_to_api(relation: Relation) -> JSON:
    return _typed("person", relation.person, relation.type)


def address_to_api(address: Address) -> JSON:
    item: JSON = {}
    _put(item, "formattedValue", address.formatted)
    _put(item, "streetAddress", address.street)
    _put(item, "extendedAddress", address.extended)
    _put(item, "poBox", address.po_box)
    _put(item, "city", address.city)
    _put(item, "region", address.region)
    _put(item, "postalCode", address.postal_code)
    _put(item, "country", address.country)
    _put(item, "countryCode", address.country_code)
    _put(item, "type", address.type)
    return item


def organization_to_api(org: Organization) -> JSON:
    item: JSON = {}
    _put(item, "name", org.name)
    _put(item, "title", org.title)
    _put(item, "department", org.department)
    _put(item, "type", org.type)
    if org.current is not None:
        item["current"] = org.current
    return item


def event_to_api(event: Event) -> JSON:
    item: JSON = {"date": date_to_api(event.date)}
    _put(item, "type", event.type)
    return item


def user_defined_to_api(ud: UserDefined) -> JSON:
    return {"key": ud.key.strip(), "value": ud.value}


def nickname_to_api(nickname: str) -> JSON:
    return {"value": nickname.strip()}


# --- match keys ------------------------------------------------------------------------


def _digits(value: Any) -> str:
    return "".join(ch for ch in value if ch.isdigit()) if isinstance(value, str) else ""


def _phone_query(value: str) -> str:
    digits = _digits(value)
    return digits or folded(value)


def _phone_keys(item: JSON) -> set[str]:
    keys = {_digits(item.get("value")), _digits(item.get("canonicalForm"))}
    if not any(keys):
        keys.add(folded(item.get("value")))
    return keys - {""}


def _date_key(date: Any) -> str:
    if not isinstance(date, dict):
        return ""
    year, month, day = (date.get(k) for k in ("year", "month", "day"))
    head = f"{year:04d}" if isinstance(year, int) and year else "-"
    parts = [head]
    if isinstance(month, int) and month:
        parts.append(f"{month:02d}")
        if isinstance(day, int) and day:
            parts.append(f"{day:02d}")
    return "-".join(parts)


_ADDRESS_PARTS = (
    "streetAddress",
    "extendedAddress",
    "poBox",
    "postalCode",
    "city",
    "region",
    "country",
)


def _address_text(item: JSON) -> str:
    formatted = item.get("formattedValue")
    if isinstance(formatted, str) and formatted.strip():
        return formatted
    return ", ".join(
        str(item[k]) for k in _ADDRESS_PARTS if isinstance(item.get(k), str) and item[k].strip()
    )


def _org_text(item: JSON) -> str:
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name
    title = item.get("title")
    return title if isinstance(title, str) else ""


def _keys_of(text_of: Callable[[JSON], Any]) -> Callable[[JSON], set[str]]:
    def keys(item: JSON) -> set[str]:
        return {folded(text_of(item))} - {""}

    return keys


# --- field specs -----------------------------------------------------------------------


@dataclass(frozen=True)
class ListField:
    """How one multi-valued field is written, read back, and matched."""

    arg: str  # tool argument name ("emails")
    api: str  # People API field name ("emailAddresses")
    to_api: Callable[[Any], JSON]
    from_api: Callable[[JSON], Any]  # → model, str, or None (unreadable item)
    keys: Callable[[JSON], set[str]]  # normalized match keys of a raw item
    query: Callable[[str], str]  # normalizes a `remove` / `set_primary` value or a `set` key
    label: Callable[[JSON], str]  # how an item is shown in an error
    # `add` doesn't duplicate a value the contact already has (it may retype it).
    dedupe_by_key: bool = False


def _value(item: JSON) -> Any:
    return item.get("value")


LIST_FIELDS: tuple[ListField, ...] = (
    ListField(
        "nicknames",
        "nicknames",
        nickname_to_api,
        lambda i: clean(i["value"]).strip() or None if isinstance(i.get("value"), str) else None,
        _keys_of(_value),
        folded,
        lambda i: str(i.get("value", "")),
        dedupe_by_key=True,
    ),
    ListField(
        "emails",
        "emailAddresses",
        email_to_api,
        email_from_api,
        _keys_of(_value),
        folded,
        lambda i: str(i.get("value", "")),
        dedupe_by_key=True,
    ),
    ListField(
        "phones",
        "phoneNumbers",
        phone_to_api,
        phone_from_api,
        _phone_keys,
        _phone_query,
        lambda i: str(i.get("value", "")),
        dedupe_by_key=True,
    ),
    ListField(
        "organizations",
        "organizations",
        organization_to_api,
        organization_from_api,
        _keys_of(_org_text),
        folded,
        _org_text,
    ),
    ListField(
        "addresses",
        "addresses",
        address_to_api,
        address_from_api,
        _keys_of(_address_text),
        folded,
        lambda i: " ".join(_address_text(i).split()),
    ),
    ListField(
        "urls",
        "urls",
        url_to_api,
        url_from_api,
        _keys_of(_value),
        folded,
        lambda i: str(i.get("value", "")),
        dedupe_by_key=True,
    ),
    ListField(
        "relations",
        "relations",
        relation_to_api,
        relation_from_api,
        _keys_of(lambda i: i.get("person")),
        folded,
        lambda i: str(i.get("person", "")),
    ),
    ListField(
        "events",
        "events",
        event_to_api,
        event_from_api,
        lambda i: {_date_key(i.get("date"))} - {""},
        lambda q: q.strip(),
        lambda i: _date_key(i.get("date")),
    ),
    ListField(
        "user_defined",
        "userDefined",
        user_defined_to_api,
        user_defined_from_api,
        _keys_of(lambda i: i.get("key")),
        folded,
        lambda i: str(i.get("key", "")),
    ),
)
LIST_FIELD_BY_ARG = {f.arg: f for f in LIST_FIELDS}

#: Tool argument → People API field, for every writable field.
API_FIELD = {
    **{f.arg: f.api for f in LIST_FIELDS},
    "name": "names",
    "birthday": "birthdays",
    "biography": "biographies",
}
ARG_FIELD = {api: arg for arg, api in API_FIELD.items()}


# --- helpers ---------------------------------------------------------------------------


def _items(person: JSON, api_field: str) -> list[JSON]:
    items = person.get(api_field)
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def _flagged(item: JSON) -> bool:
    meta = item.get("metadata")
    return isinstance(meta, dict) and (
        meta.get("primary") is True or meta.get("sourcePrimary") is True
    )


def _set_flags(item: JSON, primary: bool) -> JSON:
    out = dict(item)
    meta = dict(out["metadata"]) if isinstance(out.get("metadata"), dict) else {}
    # ``primary`` is what Google reports; ``sourcePrimary`` is what it honours on
    # write for the contact source. Set both, and put the primary item first.
    meta["primary"] = primary
    meta["sourcePrimary"] = primary
    out["metadata"] = meta
    return out


def make_primary(items: list[JSON], chosen: JSON) -> list[JSON]:
    """``chosen`` (an element of ``items``) first and primary, the others not primary.

    Returns ``items`` unchanged when ``chosen`` already is the only primary item.
    """
    others = [i for i in items if i is not chosen]
    if is_primary(chosen) and not any(_flagged(i) for i in others):
        return items
    return [_set_flags(chosen, True)] + [_set_flags(i, False) if _flagged(i) else i for i in others]


_PROFILE_ONLY = "comes from the person's own Google profile, not this contact: it can't be"


def _listing(spec: ListField, items: Sequence[JSON]) -> str:
    labels = [clean(spec.label(i)) for i in items]
    labels = [label for label in labels if label]
    if not labels:
        return "(none)"
    return ", ".join(f"'{label}'" for label in labels[:50]) + (
        f", … ({len(labels)} in all)" if len(labels) > 50 else ""
    )


def _model_primary(model: Any) -> bool:
    return bool(getattr(model, "primary", False))


def _build_list(spec: ListField, models: Sequence[Any], errors: list[str]) -> list[JSON]:
    """New items from models; the one marked ``primary`` (at most one) goes first."""
    built = [spec.to_api(m) for m in models]
    flagged = [b for b, m in zip(built, models, strict=True) if _model_primary(m)]
    if len(flagged) > 1:
        errors.append(f"{spec.arg}: at most one value can be primary.")
        return built
    if flagged:
        return make_primary(built, flagged[0])
    return built


def _retyped(existing: JSON, added: JSON) -> JSON:
    """``existing`` with the type of ``added`` when ``added`` names a different one.

    Types compare case-insensitively against ``type``, or Google's ``formattedType`` label
    when the item has no ``type``. The returned copy drops ``formattedType``, which Google
    recomputes from the new type. Without a type on ``added``, ``existing`` is returned.
    """
    wanted = added.get("type")  # already stripped, never empty (see _typed)
    if not isinstance(wanted, str):
        return existing
    current = existing.get("type") or existing.get("formattedType")
    if isinstance(current, str) and current.strip().casefold() == wanted.casefold():
        return existing
    retyped = copy.deepcopy(existing)
    retyped["type"] = wanted
    retyped.pop("formattedType", None)
    return retyped


def apply_list_ops(
    spec: ListField, items: list[JSON], ops: ListOps[Any], errors: list[str]
) -> list[JSON]:
    """The new raw item list for one multi-valued field. Problems go to ``errors``."""
    marked: JSON | None = None
    if ops.replace_all is not None:
        new = [spec.to_api(m) for m in ops.replace_all]
        primaries = [b for b, m in zip(new, ops.replace_all, strict=True) if _model_primary(m)]
    else:
        drop: list[JSON] = []
        for value in ops.remove:
            query = spec.query(value)
            matched = [i for i in items if query and query in spec.keys(i)]
            own = [i for i in matched if is_contact_data(i)]
            if matched and not own:
                errors.append(f"{spec.arg}: '{clean(value)}' {_PROFILE_ONLY} removed here.")
            elif not matched:
                errors.append(
                    f"{spec.arg}: remove '{clean(value)}' matches no existing value. "
                    f"Existing: {_listing(spec, items)}."
                )
            drop.extend(own)
        new = [i for i in items if not any(i is d for d in drop)]
        primaries = []
        for model in ops.add:
            item = spec.to_api(model)
            if spec.dedupe_by_key:
                keys = spec.keys(item)
                dupe = next((i for i in new if is_contact_data(i) and keys & spec.keys(i)), None)
                if dupe is not None:
                    # Already there: adding it again would make a duplicate. A different
                    # type is a change to the existing value, not a second value.
                    retyped = _retyped(dupe, item)
                    if retyped is not dupe:
                        new = [retyped if i is dupe else i for i in new]
                        dupe = retyped
                    if _model_primary(model):
                        primaries.append(dupe)
                    continue
            new.append(item)
            if _model_primary(model):
                primaries.append(item)
    if len(primaries) > 1:
        errors.append(f"{spec.arg}: at most one value can be primary.")
        return new
    if primaries:
        marked = primaries[0]
    if ops.set_primary is not None:
        query = spec.query(ops.set_primary)
        matched = [i for i in new if query and query in spec.keys(i)]
        if matched and not any(is_contact_data(i) for i in matched):
            errors.append(
                f"{spec.arg}: set_primary '{clean(ops.set_primary)}' {_PROFILE_ONLY} made "
                "primary here."
            )
            return new
        matched = [i for i in matched if is_contact_data(i)]
        if len(matched) != 1:
            what = "matches no value" if not matched else f"matches {len(matched)} values"
            errors.append(
                f"{spec.arg}: set_primary '{clean(ops.set_primary)}' {what}; it must match "
                f"exactly one. Values: {_listing(spec, new)}."
            )
            return new
        if marked is not None and marked is not matched[0]:
            errors.append(f"{spec.arg}: set_primary and a value marked primary disagree.")
            return new
        marked = matched[0]
    if marked is not None:
        new = make_primary(new, marked)
    return new


def set_by_key(items: list[JSON], ops: CustomFieldOps) -> list[JSON]:
    """``items`` with each key of ``ops.set`` holding the given value: the key's first own
    item takes it and keeps its place, metadata and spelling, later items with that key
    go, and a new key is appended. ``items`` already holds the result of add and remove."""
    spec = LIST_FIELD_BY_ARG["user_defined"]
    new = list(items)
    for pair in ops.set:
        query = spec.query(pair.key)
        matched = [i for i in new if is_contact_data(i) and query in spec.keys(i)]
        if not matched:
            new.append(spec.to_api(pair))
            continue
        first, *extra = matched
        kept = first if first.get("value") == pair.value else {**first, "value": pair.value}
        new = [kept if i is first else i for i in new if not any(i is e for e in extra)]
    return new


def _edit_target(items: list[JSON]) -> tuple[int | None, JSON]:
    """The item a single-valued field's change edits: ``(index, copy)`` of the contact
    source's item (the primary one first). Google ignores non-contact data (e.g. a
    linked Google profile's name) on update, so without a contact item the result is
    ``(None, base)``: a new item seeded from the primary item's content."""
    order = sorted(range(len(items)), key=lambda n: not is_primary(items[n]))
    for n in order:
        if is_contact_data(items[n]):
            return n, copy.deepcopy(items[n])
    if order:
        base = copy.deepcopy(items[order[0]])
        base.pop("metadata", None)
        return None, base
    return None, {}


def _replace_at(items: list[JSON], index: int | None, item: JSON) -> list[JSON]:
    if index is None:
        return [*items, item]
    return [item if n == index else i for n, i in enumerate(items)]


def merge_name(items: list[JSON], name: NameFields, errors: list[str]) -> list[JSON]:
    """Merge the given name parts into the contact's name item (the rest is kept)."""
    parts = {
        _NAME_PARTS[attr]: value
        for attr in _NAME_PARTS
        if (value := getattr(name, attr)) is not None
    }
    if not parts:
        errors.append("name: give at least one part (given, family, middle, prefix, suffix).")
        return items
    index, item = _edit_target(items)
    changed = False
    for key, value in parts.items():
        value = value.strip()
        if value:
            changed |= item.get(key) != value
            item[key] = value
        elif key in item:
            changed = True
            del item[key]
    if index is None:
        # A new name item for the contact: only if it has something in it.
        if not any(item.get(key) for key in _NAME_PARTS.values()):
            return items
    elif not changed:
        return items
    for key in _DERIVED_NAME_KEYS:
        item.pop(key, None)
    return _replace_at(items, index, item)


def replace_birthday(items: list[JSON], date: PartialDate) -> list[JSON]:
    index, item = _edit_target(items)
    new_date = date_to_api(date)
    if index is not None and item.get("date") == new_date and "text" not in item:
        return items
    item["date"] = new_date
    item.pop("text", None)  # the free-text form would contradict the new date
    return _replace_at(items, index, item)


def replace_biography(items: list[JSON], text: str) -> list[JSON]:
    if not text.strip():
        return []
    index, item = _edit_target(items)
    if (
        index is not None
        and item.get("value") == text
        and item.get("contentType", "TEXT_PLAIN") == "TEXT_PLAIN"
    ):
        return items
    item["value"] = text
    item["contentType"] = "TEXT_PLAIN"
    return _replace_at(items, index, item)


# --- normalized view (for FieldChange) -------------------------------------------------


def _dump(model: Any) -> JsonValue:
    out: JsonValue = json_dump(model)
    return out


def normalized(api_field: str, items: list[JSON]) -> JsonValue:
    """The tool-facing value of one field, as in ``Contact`` (primary first)."""
    ordered = primary_first(items)
    if api_field == "names":
        return _dump(name_from_api(ordered[0])) if ordered else None
    if api_field == "birthdays":
        for item in ordered:
            date = date_from_api(item.get("date"))
            if date is not None:
                return _dump(date)
        return None
    if api_field == "biographies":
        value = ordered[0].get("value") if ordered else None
        return clean(value) if isinstance(value, str) else None
    spec = LIST_FIELD_BY_ARG[ARG_FIELD[api_field]]
    out: list[JsonValue] = []
    for item in ordered:
        model = spec.from_api(item)
        if model is None:
            continue
        out.append(model if isinstance(model, str) else _dump(model))
    return out


# --- planning --------------------------------------------------------------------------


@dataclass
class UpdatePlan:
    """What an update changes. ``fields`` holds the new raw lists of changed fields."""

    fields: dict[str, list[JSON]] = field(default_factory=dict)
    changes: list[FieldChange] = field(default_factory=list)

    @property
    def update_fields(self) -> list[str]:
        """``updatePersonFields``: the changed People API fields, in a stable order."""
        return [f for f in WRITABLE_FIELDS if f in self.fields]

    def body(self, person: JSON, mask: Sequence[str] | None = None) -> JSON:
        """The ``updateContact`` person: identity, etag and source metadata (Google needs
        ``metadata.sources`` to find the contact source), plus every field in ``mask``
        (default: the changed ones); unchanged fields carry their current value."""
        out: JSON = {"resourceName": person.get("resourceName"), "etag": person.get("etag")}
        if isinstance(person.get("metadata"), dict):
            out["metadata"] = person["metadata"]
        for api_field in mask if mask is not None else self.update_fields:
            out[api_field] = self.fields.get(api_field, _items(person, api_field))
        return out


def _check_custom_field_set(ops: CustomFieldOps) -> None:
    query = LIST_FIELD_BY_ARG["user_defined"].query
    keys = [query(pair.key) for pair in ops.set]
    if not all(keys):
        raise InvalidArgumentError("user_defined: set needs a key that is not blank.")
    others = {query(key) for key in ops.remove} | {query(m.key) for m in ops.add}
    conflicting = {
        clean(pair.key)
        for pair, key in zip(ops.set, keys, strict=True)
        if keys.count(key) > 1 or key in others
    }
    if conflicting:
        raise InvalidArgumentError(
            "user_defined: set takes each key once, not also in add or remove: "
            + ", ".join(f"'{key}'" for key in sorted(conflicting))
            + "."
        )


def check_ops(ops: ContactOps) -> None:
    """Checks that need no person: something to do, no clear/set conflict, a usable `set`."""
    given = [f for f in ContactOps.model_fields if f != "clear" and getattr(ops, f) is not None]
    if not given and not ops.clear:
        raise InvalidArgumentError("Nothing to update: give at least one field to change.")
    conflicts = [c for c in ops.clear if getattr(ops, c) is not None]
    if conflicts:
        raise InvalidArgumentError(
            "Both set and cleared: " + ", ".join(sorted(set(conflicts))) + ". Do one or the other."
        )
    if ops.user_defined is not None:
        _check_custom_field_set(ops.user_defined)


def plan_update(person: JSON, ops: ContactOps) -> UpdatePlan:
    """Apply ``ops`` to ``person`` (not modified). Raises InvalidArgumentError, listing
    every problem, before anything is written."""
    check_ops(ops)
    errors: list[str] = []
    new: dict[str, list[JSON]] = {}

    if ops.name is not None:
        new["names"] = merge_name(_items(person, "names"), ops.name, errors)
    for spec in LIST_FIELDS:
        list_ops = getattr(ops, spec.arg)
        if list_ops is not None:
            new[spec.api] = apply_list_ops(spec, _items(person, spec.api), list_ops, errors)
    if ops.user_defined is not None and ops.user_defined.set:
        new["userDefined"] = set_by_key(new["userDefined"], ops.user_defined)
    if ops.birthday is not None:
        new["birthdays"] = replace_birthday(_items(person, "birthdays"), ops.birthday)
    if ops.biography is not None:
        new["biographies"] = replace_biography(_items(person, "biographies"), ops.biography)
    for cleared in ops.clear:
        new[API_FIELD[cleared]] = []
    if errors:
        raise InvalidArgumentError("Nothing was changed. " + " ".join(errors))

    plan = UpdatePlan()
    for api_field in WRITABLE_FIELDS:
        if api_field not in new:
            continue
        before = _items(person, api_field)
        after = new[api_field]
        if after == before:
            continue
        plan.fields[api_field] = after
        before_n, after_n = normalized(api_field, before), normalized(api_field, after)
        plan.changes.append(FieldChange(field=ARG_FIELD[api_field], before=before_n, after=after_n))
    return plan


def build_person(contact: ContactFields, group_resource_names: Sequence[str] = ()) -> JSON:
    """The ``createContact`` person for a new contact. Raises InvalidArgumentError."""
    errors: list[str] = []
    person: JSON = {}
    if contact.name is not None:
        name = name_to_api(contact.name)
        if name:
            person["names"] = [name]
    for spec in LIST_FIELDS:
        models = getattr(contact, spec.arg)
        if models:
            person[spec.api] = _build_list(spec, models, errors)
    if contact.birthday is not None:
        person["birthdays"] = [{"date": date_to_api(contact.birthday)}]
    if contact.biography is not None and contact.biography.strip():
        person["biographies"] = [{"value": contact.biography, "contentType": "TEXT_PLAIN"}]
    if errors:
        raise InvalidArgumentError(" ".join(errors))
    if not person:
        raise InvalidArgumentError("A new contact needs at least one field (e.g. a name).")
    if group_resource_names:
        person["memberships"] = [
            {"contactGroupMembership": {"contactGroupResourceName": rn}}
            for rn in group_resource_names
        ]
    return person


def requested_fields(ops: ContactOps) -> list[str]:
    """People API fields an update asks to change (for the audit line of a refused one)."""
    args = {f for f in API_FIELD if getattr(ops, f) is not None} | set(ops.clear)
    return [API_FIELD[a] for a in sorted(args)]


def touched_fields(contact: ContactFields) -> list[str]:
    """People API fields a new contact sets (for the audit line)."""
    out = [api for arg, api in API_FIELD.items() if getattr(contact, arg)]
    if contact.groups:
        out.append("memberships")
    return out
