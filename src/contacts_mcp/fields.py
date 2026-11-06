"""People API field masks, in one place so reads and writes agree."""

from __future__ import annotations

from typing import Final

PEOPLE_API_URL: Final = "https://people.googleapis.com/v1"

SCOPES: Final = (
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/contacts.other.readonly",
)

#: Every person field this server writes and reads back (``personFields`` on writes).
PERSON_FIELDS: Final = (
    "names",
    "nicknames",
    "emailAddresses",
    "phoneNumbers",
    "organizations",
    "addresses",
    "birthdays",
    "biographies",
    "urls",
    "relations",
    "events",
    "memberships",
    "userDefined",
    "photos",
)

#: ``personFields`` of ``get_contact``. ``metadata`` stays out of ``PERSON_FIELDS``: it is
#: read-only, and the write path neither needs it nor may treat it as writable.
READ_FIELDS: Final = (*PERSON_FIELDS, "metadata")

#: ``personFields`` of listings, whose summaries add the details only a full read can tell.
LISTING_FIELDS: Final = (*(f for f in PERSON_FIELDS if f != "photos"), "metadata")

#: The subset a plain summary needs (search and write results).
SUMMARY_FIELDS: Final = (
    "names",
    "emailAddresses",
    "phoneNumbers",
    "organizations",
    "memberships",
)

#: Fields ``updateContact`` / ``createContact`` may write (``updatePersonFields``).
#: Memberships are written only through the group tools; photos are read-only.
WRITABLE_FIELDS: Final = tuple(f for f in PERSON_FIELDS if f not in ("memberships", "photos"))

#: ``readMask`` for listing *Other contacts*: the API accepts only these.
OTHER_CONTACT_FIELDS: Final = ("names", "emailAddresses", "phoneNumbers", "photos")

#: ``readMask`` for ``otherContacts:search``, which does not accept ``photos``.
OTHER_CONTACT_SEARCH_FIELDS: Final = ("names", "emailAddresses", "phoneNumbers")

#: ``copyMask`` for copyOtherContactToMyContactsGroup.
OTHER_CONTACT_COPY_FIELDS: Final = ("names", "emailAddresses", "phoneNumbers")

#: System contact groups addressable by the group tools.
SYSTEM_GROUPS: Final = ("contactGroups/myContacts", "contactGroups/starred")


def mask(fields: tuple[str, ...]) -> str:
    return ",".join(fields)
