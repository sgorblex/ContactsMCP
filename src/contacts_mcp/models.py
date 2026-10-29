"""Tool input and output models.

Every model forbids unknown fields, so a misspelled argument is refused by name
instead of silently changing what a write does. Output strings are sanitized by
the normalizer (:mod:`contacts_mcp.normalize`), not here.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- resource names -------------------------------------------------------------------

_RESOURCE_RE = {
    "people/": re.compile(r"people/[0-9A-Za-z_-]{1,128}"),
    "contactGroups/": re.compile(r"contactGroups/[0-9A-Za-z_-]{1,128}"),
    "otherContacts/": re.compile(r"otherContacts/[0-9A-Za-z_-]{1,128}"),
}


def check_resource_name(value: str, prefix: str) -> str:
    """Validate a ``people/…``, ``contactGroups/…`` or ``otherContacts/…`` name."""
    value = value.strip()
    if not _RESOURCE_RE[prefix].fullmatch(value):
        raise ValueError(f"must be a resource name like '{prefix}c1234567890', got {value!r}")
    return value


def _person(v: str) -> str:
    v = v.strip()
    if v == "people/me":
        raise ValueError("people/me is the account owner's profile, not a contact")
    return check_resource_name(v, "people/")


def _other(v: str) -> str:
    return check_resource_name(v, "otherContacts/")


PersonResourceName = Annotated[
    str,
    AfterValidator(_person),
    Field(description="Contact resource name, e.g. 'people/c1234567890'."),
]
OtherContactResourceName = Annotated[
    str,
    AfterValidator(_other),
    Field(description="Other-contact resource name, e.g. 'otherContacts/c1234567890'."),
]

AccountArg = Annotated[
    str | None,
    Field(
        description=(
            "Account alias (see the `accounts` tool). May be omitted only when exactly one "
            "account is configured."
        )
    ),
]


# --- contact fields (read and write) --------------------------------------------------


class PartialDate(Model):
    """A date whose year (or day) may be unknown, e.g. a birthday without a year."""

    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if self.year is None and self.month is None:
            raise ValueError("a date needs at least a year or a month")
        if self.day is not None and self.month is None:
            raise ValueError("a date with a day needs a month")
        return self


class NameFields(Model):
    """The parts of a person's name. On update, only the parts given are replaced."""

    given: str | None = None
    family: str | None = None
    middle: str | None = None
    prefix: str | None = Field(default=None, description="Honorific prefix, e.g. 'Dr.'")
    suffix: str | None = Field(default=None, description="Honorific suffix, e.g. 'Jr.'")


class Name(NameFields):
    display: str | None = Field(default=None, description="Display name (read only).")


_TYPE_DESC = "Type label: 'home', 'work', 'other', or any custom label."
_PRIMARY_DESC = "Whether this is the primary value of its kind."


class Email(Model):
    value: str = Field(min_length=3)
    type: str | None = Field(default=None, description=_TYPE_DESC)
    primary: bool = Field(default=False, description=_PRIMARY_DESC)


class Phone(Model):
    value: str = Field(min_length=1)
    type: str | None = Field(
        default=None, description="'mobile', 'home', 'work', 'main', 'other', or custom."
    )
    primary: bool = Field(default=False, description=_PRIMARY_DESC)
    canonical: str | None = Field(default=None, description="E.164 form (read only).")


class Address(Model):
    formatted: str | None = Field(
        default=None, description="The full address as one string (read; or write it alone)."
    )
    street: str | None = None
    extended: str | None = Field(default=None, description="Apartment, suite, etc.")
    po_box: str | None = None
    city: str | None = None
    region: str | None = Field(default=None, description="State, province or region.")
    postal_code: str | None = None
    country: str | None = None
    country_code: str | None = Field(default=None, description="ISO 3166-1 alpha-2.")
    type: str | None = Field(default=None, description=_TYPE_DESC)
    primary: bool = Field(default=False, description=_PRIMARY_DESC)


class Organization(Model):
    name: str | None = Field(default=None, description="Company or organization name.")
    title: str | None = Field(default=None, description="Job title.")
    department: str | None = None
    type: str | None = Field(default=None, description="'work', 'school', or custom.")
    current: bool | None = None
    primary: bool = Field(default=False, description=_PRIMARY_DESC)


class Url(Model):
    value: str = Field(min_length=1)
    type: str | None = Field(
        default=None, description="'home', 'work', 'blog', 'profile', 'homePage', or custom."
    )
    primary: bool = Field(default=False, description=_PRIMARY_DESC)


class Relation(Model):
    person: str = Field(min_length=1, description="The related person's name.")
    type: str | None = Field(
        default=None, description="'spouse', 'child', 'mother', 'friend', 'manager', or custom."
    )


class Event(Model):
    date: PartialDate
    type: str | None = Field(default=None, description="'anniversary', 'other', or custom.")


class UserDefined(Model):
    key: str = Field(min_length=1)
    value: str


class GroupRef(Model):
    resource_name: str
    name: str


# --- output: contacts ------------------------------------------------------------------


class ContactSummary(Model):
    """The compact form returned by list and search tools."""

    resource_name: str
    account: str | None = Field(default=None, description="Account alias (multi-account search).")
    display_name: str | None = None
    email: str | None = Field(default=None, description="Primary e-mail address.")
    phone: str | None = Field(default=None, description="Primary phone number.")
    organization: str | None = Field(default=None, description="'Title, Company' of the primary.")
    groups: list[str] = Field(default_factory=list, description="Contact group (label) names.")


class Contact(Model):
    """The full normalized contact returned by ``get_contact`` and write tools."""

    resource_name: str
    etag: str | None = None
    account: str | None = None
    name: Name | None = None
    nicknames: list[str] = Field(default_factory=list)
    emails: list[Email] = Field(default_factory=list)
    phones: list[Phone] = Field(default_factory=list)
    organizations: list[Organization] = Field(default_factory=list)
    addresses: list[Address] = Field(default_factory=list)
    birthday: PartialDate | None = None
    birthday_text: str | None = Field(
        default=None, description="A birthday Google holds only as free text."
    )
    biography: str | None = Field(default=None, description="Notes.")
    urls: list[Url] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    user_defined: list[UserDefined] = Field(default_factory=list, description="Custom fields.")
    groups: list[GroupRef] = Field(default_factory=list)
    photo_url: str | None = None
    from_profile: list[str] = Field(
        default_factory=list,
        description=(
            "Values above that come from the person's own Google profile, not this contact: "
            "read only, so they can't be changed or removed here."
        ),
    )
    raw: dict[str, JsonValue] | None = Field(
        default=None, description="The People API person object (only with raw=true)."
    )


class OtherContact(Model):
    """An *Other contact*: created by Google from people the account interacted with."""

    resource_name: str
    account: str | None = None
    display_name: str | None = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)


# --- output: results -------------------------------------------------------------------

AccountStatusName = Literal["ok", "needs_relink", "unreadable"]


class AccountInfo(Model):
    alias: str
    email: str | None = None
    read_only: bool = False
    status: AccountStatusName
    detail: str | None = Field(default=None, description="What to do when status is not ok.")


class AccountsResult(Model):
    accounts: list[AccountInfo]
    default_account: str | None = Field(
        default=None, description="The account used when `account` is omitted (only one)."
    )


class AccountFailure(Model):
    account: str
    error: str


class ContactList(Model):
    contacts: list[ContactSummary]
    next_page_token: str | None = Field(
        default=None, description="Pass as page_token for the next page; absent on the last."
    )
    total_items: int | None = None


class SearchResult(Model):
    contacts: list[ContactSummary]
    failures: list[AccountFailure] = Field(
        default_factory=list, description="Accounts that could not be searched (with '*')."
    )


class FieldChange(Model):
    field: str
    before: JsonValue
    after: JsonValue


class UpdateResult(Model):
    resource_name: str
    etag: str | None
    changes: list[FieldChange]
    contact: ContactSummary


class DeleteResult(Model):
    deleted: ContactSummary


class Group(Model):
    resource_name: str
    name: str
    type: Literal["user", "system"]
    member_count: int | None = None


class GroupList(Model):
    groups: list[Group]


class DeleteGroupResult(Model):
    deleted: Group
    contacts_deleted: int = Field(description="Contacts deleted with the group (0 unless asked).")


class GroupMembershipResult(Model):
    group: GroupRef
    changed: list[str] = Field(description="Contacts added to / removed from the group.")
    unchanged: list[str] = Field(
        default_factory=list,
        description="Contacts already in the group (add) or not in it (remove): nothing to do.",
    )
    not_found: list[str] = Field(
        default_factory=list, description="Resource names Google did not find."
    )
    could_not_remove: list[str] = Field(
        default_factory=list,
        description="Contacts not removed (Google refuses removing a contact's last group).",
    )


class OtherContactList(Model):
    other_contacts: list[OtherContact]
    next_page_token: str | None = None


class OtherContactSearchResult(Model):
    other_contacts: list[OtherContact]


# --- input: update operations ----------------------------------------------------------


class ListOps[T](Model):
    """Explicit operations on a multi-valued field. Existing values are never dropped
    silently: use ``remove`` (by value) or ``replace_all``.

    ``remove`` and ``set_primary`` match by value: e-mails case-insensitively, phone
    numbers by digits only, other fields by trimmed, case-insensitive text (the
    address's formatted text, the organization's name, the relation's person, the
    custom field's key, the event's date as YYYY-MM-DD or --MM-DD). A ``remove``
    that matches nothing is an error.
    """

    add: list[T] = Field(default_factory=list, description="Values to append.")
    remove: list[str] = Field(default_factory=list, description="Values to remove, by value.")
    replace_all: list[T] | None = Field(
        default=None, description="Replace every existing value with these ([] clears)."
    )
    set_primary: str | None = Field(
        default=None, description="Make the value matching this the primary one."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.replace_all is not None and (self.add or self.remove):
            raise ValueError("use replace_all alone, or add/remove, not both")
        if self.replace_all is None and not (self.add or self.remove or self.set_primary):
            raise ValueError("give at least one of add, remove, replace_all, set_primary")
        return self


ClearableField = Literal["name", "birthday", "biography"]


def json_dump(model: BaseModel | None) -> Any:
    """A model as JSON-compatible data, without unset defaults (for FieldChange)."""
    if model is None:
        return None
    return model.model_dump(mode="json", exclude_defaults=True)
