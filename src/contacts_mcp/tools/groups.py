"""Tools: ``list_groups``, ``create_group``, ``rename_group``, ``delete_group``,
``add_to_group``, ``remove_from_group``; and the group resolver the write tools share.

Groups are referred to by resource name (``contactGroups/…``) or exact name
(case-insensitive); the system groups also by ``myContacts`` / ``starred``. System
groups are never renamed or deleted, and only ``myContacts`` and ``starred`` accept
member changes: both refused here, before Google is called.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from contacts_mcp.errors import InvalidArgumentError, NotFoundError
from contacts_mcp.fields import SYSTEM_GROUPS
from contacts_mcp.models import (
    AccountArg,
    DeleteGroupResult,
    Group,
    GroupList,
    GroupMembershipResult,
    GroupRef,
    PersonResourceName,
    check_resource_name,
)
from contacts_mcp.normalize import group_to_model
from contacts_mcp.registry import Account, AccountRegistry
from contacts_mcp.sanitize import clean
from contacts_mcp.tools._common import (
    DESTRUCTIVE,
    DESTRUCTIVE_IDEMPOTENT,
    READ,
    UNTRUSTED_NOTE,
    WRITE,
    WRITE_IDEMPOTENT,
    write_attempt,
)

JSON = dict[str, Any]

#: Short names for the system groups that matter (case-insensitive).
SYSTEM_ALIASES = {
    "mycontacts": "contactGroups/myContacts",
    "starred": "contactGroups/starred",
}
#: contactGroups/members:modify takes at most this many resource names.
MEMBERS_LIMIT = 1000

GroupArg = Annotated[
    str,
    Field(
        min_length=1,
        description=(
            "The group: its resource name ('contactGroups/…'), its exact name "
            "(case-insensitive), or 'myContacts' / 'starred'."
        ),
    ),
]
GroupNameArg = Annotated[
    str, Field(min_length=1, max_length=200, description="The group's name (label).")
]


@dataclass(frozen=True)
class ResolvedGroup:
    resource_name: str
    name: str
    system: bool
    raw: JSON

    def ref(self) -> GroupRef:
        return GroupRef(resource_name=self.resource_name, name=self.name)

    def model(self) -> Group:
        return group_to_model(self.raw)


def _resolved(raw: JSON) -> ResolvedGroup:
    model = group_to_model(raw)
    return ResolvedGroup(model.resource_name, model.name, model.type == "system", raw)


def _names(raw: JSON) -> set[str]:
    return {
        clean(v).strip().casefold()
        for v in (raw.get("name"), raw.get("formattedName"))
        if isinstance(v, str) and v.strip()
    }


def resolve_group(groups: Sequence[JSON], ref: str) -> ResolvedGroup:
    """Find ``ref`` among the account's groups (pure; ``groups`` from ``list_groups``)."""
    ref = ref.strip()
    by_rn = {g["resourceName"]: g for g in groups if isinstance(g.get("resourceName"), str)}
    rn = SYSTEM_ALIASES.get(ref.casefold())
    if rn is None and ref.startswith("contactGroups/"):
        try:
            rn = check_resource_name(ref, "contactGroups/")
        except ValueError as e:
            raise InvalidArgumentError(f"group: {e}") from None
    if rn is not None:
        found = by_rn.get(rn)
        if found is None:
            raise NotFoundError(f"No contact group {rn} in this account.", status=404)
        return _resolved(found)
    wanted = ref.casefold()
    matches = [g for g in by_rn.values() if wanted in _names(g)]
    if len(matches) == 1:
        return _resolved(matches[0])
    if matches:
        listed = ", ".join(f"{_resolved(g).name} ({g['resourceName']})" for g in matches)
        raise InvalidArgumentError(
            f"Several groups are named '{clean(ref)}': {listed}. Pass the resource name."
        )
    names = sorted(_resolved(g).name for g in by_rn.values())
    shown = ", ".join(f"'{n}'" for n in names[:50]) or "(none)"
    raise InvalidArgumentError(f"No group named '{clean(ref)}'. Groups: {shown}.")


def check_members_writable(group: ResolvedGroup) -> None:
    if group.system and group.resource_name not in SYSTEM_GROUPS:
        raise InvalidArgumentError(
            f"System group '{group.name}' ({group.resource_name}) cannot have members changed; "
            "among system groups only myContacts and starred can."
        )


def check_user_group(group: ResolvedGroup, action: str) -> None:
    if group.system:
        raise InvalidArgumentError(
            f"'{group.name}' ({group.resource_name}) is a system group: it cannot be {action}."
        )


async def resolve_groups(account: Account, refs: Sequence[str]) -> list[ResolvedGroup]:
    """Resolve several refs with one listing (none when ``refs`` is empty)."""
    if not refs:
        return []
    groups = await account.people.list_groups()
    return [resolve_group(groups, ref) for ref in refs]


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


ResourceNamesArg = Annotated[
    list[PersonResourceName],
    Field(min_length=1, max_length=MEMBERS_LIMIT, description="Contacts ('people/…')."),
]


def register(mcp: FastMCP, registry: AccountRegistry) -> None:
    """Register this module's tools on ``mcp``."""

    @mcp.tool(
        annotations=READ,
        description=(
            "List the account's contact groups (labels), user and system, with member "
            f"counts. {UNTRUSTED_NOTE}"
        ),
    )
    async def list_groups(account: AccountArg = None) -> GroupList:
        acct = registry.resolve(account)
        groups = await acct.people.list_groups()
        return GroupList(groups=[group_to_model(g) for g in groups])

    @mcp.tool(
        annotations=WRITE,
        description="Create a contact group (label). Refused if a group already has that name.",
    )
    async def create_group(name: GroupNameArg, account: AccountArg = None) -> Group:
        async with write_attempt(
            registry, tool="create_group", account=account, fields=("name",)
        ) as attempt:
            acct = attempt.acct
            name = name.strip()
            if not name:
                raise InvalidArgumentError("name: must not be blank.")
            existing = await acct.people.list_groups()
            clash = [g for g in existing if name.casefold() in _names(g)]
            if clash:
                raise InvalidArgumentError(
                    f"A group named '{clean(name)}' already exists "
                    f"({clash[0].get('resourceName')})."
                )
            try:
                created = await acct.people.create_group(name)
            finally:
                acct.invalidate_groups()
            attempt.resource_name = created.get("resourceName")
            return group_to_model(created)

    @mcp.tool(
        annotations=WRITE_IDEMPOTENT,
        description="Rename a user contact group. System groups cannot be renamed.",
    )
    async def rename_group(
        group: GroupArg, new_name: GroupNameArg, account: AccountArg = None
    ) -> Group:
        async with write_attempt(
            registry, tool="rename_group", account=account, fields=("name",)
        ) as attempt:
            acct = attempt.acct
            new_name = new_name.strip()
            if not new_name:
                raise InvalidArgumentError("new_name: must not be blank.")
            groups = await acct.people.list_groups()
            target = resolve_group(groups, group)
            attempt.resource_name = target.resource_name
            check_user_group(target, "renamed")
            clash = [
                g
                for g in groups
                if new_name.casefold() in _names(g)
                and g.get("resourceName") != target.resource_name
            ]
            if clash:
                raise InvalidArgumentError(
                    f"A group named '{clean(new_name)}' already exists "
                    f"({clash[0].get('resourceName')})."
                )
            current = await acct.people.get_group(target.resource_name)
            try:
                updated = await acct.people.update_group(
                    target.resource_name, new_name, str(current.get("etag", ""))
                )
            finally:
                acct.invalidate_groups()
            return group_to_model(updated)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Delete a user contact group. Its contacts stay (they only lose the label) unless "
            "delete_contacts is true, which deletes every contact in the group too. System "
            "groups cannot be deleted."
        ),
    )
    async def delete_group(
        group: GroupArg,
        account: AccountArg = None,
        delete_contacts: Annotated[
            bool,
            Field(description="Also delete every contact in the group (irreversible)."),
        ] = False,
    ) -> DeleteGroupResult:
        async with write_attempt(registry, tool="delete_group", account=account) as attempt:
            acct = attempt.acct
            groups = await acct.people.list_groups()
            target = resolve_group(groups, group)
            attempt.resource_name = target.resource_name
            check_user_group(target, "deleted")
            # Field names, not values: deleteContacts records that contacts went too.
            attempt.fields = (
                {"memberships", "deleteContacts"} if delete_contacts else {"memberships"}
            )
            if delete_contacts:
                # The audit line names every contact deleted with the group.
                count = target.model().member_count or 0
                members = await acct.people.get_group(
                    target.resource_name, max_members=max(count, 1)
                )
                names = members.get("memberResourceNames")
                attempt.contacts = [n for n in names if isinstance(n, str)] if names else []
            try:
                await acct.people.delete_group(
                    target.resource_name, delete_contacts=delete_contacts
                )
            finally:
                acct.invalidate_groups()
            return DeleteGroupResult(deleted=target.model(), contacts_deleted=len(attempt.contacts))

    async def _modify(
        tool: str, account: str | None, group: str, resource_names: list[str], *, add: bool
    ) -> GroupMembershipResult:
        async with write_attempt(
            registry, tool=tool, account=account, fields=("memberships",)
        ) as attempt:
            acct = attempt.acct
            target = (await resolve_groups(acct, [group]))[0]
            attempt.resource_name = target.resource_name
            check_members_writable(target)
            names = _unique(resource_names)
            attempt.contacts = list(names)
            try:
                if add:
                    body = await acct.people.modify_group_members(target.resource_name, add=names)
                else:
                    body = await acct.people.modify_group_members(
                        target.resource_name, remove=names
                    )
            finally:
                acct.invalidate_groups()
            not_found = [str(n) for n in body.get("notFoundResourceNames") or []]
            kept = [str(n) for n in body.get("canNotRemoveLastContactGroupResourceNames") or []]
            changed = [n for n in names if n not in not_found and n not in kept]
            return GroupMembershipResult(
                group=target.ref(), changed=changed, not_found=not_found, could_not_remove=kept
            )

    @mcp.tool(
        annotations=WRITE_IDEMPOTENT,
        description=(
            "Add contacts to a group (label). Works for user groups and for myContacts and "
            "starred (starring a contact). Contacts already in the group are unaffected."
        ),
    )
    async def add_to_group(
        group: GroupArg, resource_names: ResourceNamesArg, account: AccountArg = None
    ) -> GroupMembershipResult:
        return await _modify("add_to_group", account, group, resource_names, add=True)

    @mcp.tool(
        annotations=DESTRUCTIVE_IDEMPOTENT,
        description=(
            "Remove contacts from a group (label); the contacts themselves are kept. Works for "
            "user groups and for myContacts and starred (unstarring). Google refuses to remove "
            "a contact from its last group: those are listed in could_not_remove."
        ),
    )
    async def remove_from_group(
        group: GroupArg, resource_names: ResourceNamesArg, account: AccountArg = None
    ) -> GroupMembershipResult:
        return await _modify("remove_from_group", account, group, resource_names, add=False)
