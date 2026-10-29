"""People API field masks, in one place so reads and writes agree."""

from __future__ import annotations

from typing import Final

PEOPLE_API_URL: Final = "https://people.googleapis.com/v1"

SCOPES: Final = (
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/contacts.other.readonly",
)

#: Every person field this server reads and writes (``personFields`` on reads).
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

#: The subset a summary needs (list and search results).
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
