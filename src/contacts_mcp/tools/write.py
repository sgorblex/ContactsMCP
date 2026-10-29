"""Tools: ``create_contact``, ``update_contact``, ``delete_contact``,
``copy_other_contact_to_contacts``, ``batch_create_contacts``, ``batch_update_contacts``.

Every tool runs inside :func:`write_attempt` (audit line, per-account mutation lock,
read-only refusal). Updates go through :mod:`contacts_mcp.update_engine`: fetch with
``PERSON_FIELDS`` (etag), apply, send only the changed fields; on an etag conflict
re-fetch, re-apply the same operations and retry once.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from contacts_mcp.errors import (
    ContactsError,
    EtagConflictError,
    InvalidArgumentError,
    NotFoundError,
)
from contacts_mcp.fields import (
    OTHER_CONTACT_COPY_FIELDS,
    PERSON_FIELDS,
    SUMMARY_FIELDS,
    WRITABLE_FIELDS,
)
from contacts_mcp.google.people import BATCH_LIMIT, batch_item_error
from contacts_mcp.models import (
    AccountArg,
    Address,
    ClearableField,
    Contact,
    ContactSummary,
    DeleteResult,
    Email,
    Event,
    ListOps,
    Model,
    NameFields,
    Organization,
    OtherContactResourceName,
    PartialDate,
    PersonResourceName,
    Phone,
    Relation,
    UpdateResult,
    Url,
    UserDefined,
)
from contacts_mcp.normalize import person_to_contact, person_to_summary
from contacts_mcp.registry import Account, AccountRegistry
from contacts_mcp.tools._common import (
    DESTRUCTIVE,
    SEARCH_LAG_NOTE,
    UNTRUSTED_NOTE,
    WRITE,
    WriteAttempt,
    write_attempt,
)
from contacts_mcp.tools.groups import check_members_writable, resolve_groups
from contacts_mcp.update_engine import (
    JSON,
    ContactFields,
    ContactOps,
    ContactUpdate,
    UpdatePlan,
    build_person,
    check_ops,
    plan_update,
    requested_fields,
    touched_fields,
)

MY_CONTACTS = "contactGroups/myContacts"

OPS_NOTE = (
    "Multi-valued fields take operations {add, remove, replace_all, set_primary}; values "
    "not named are kept. remove and set_primary match by value: e-mails "
    "case-insensitively, phones by digits only, addresses by their formatted text, "
    "organizations by name, relations by person, custom fields by key, events by date "
    "(YYYY-MM-DD or --MM-DD), nicknames and URLs by trimmed case-insensitive text. A remove "
    "that matches nothing is an error and nothing is written. add of an e-mail, phone or URL "
    "already there with another type changes its type. name changes only the parts "
    "given; birthday and biography replace; clear empties name, birthday or biography."
)


class BatchFailure(Model):
    item: str = Field(description="`contacts[n]` (input position) or the resource name.")
    error: str


class BatchCreateResult(Model):
    contacts: list[ContactSummary] = Field(description="The new contacts, in input order.")
    failures: list[BatchFailure] = Field(
        default_factory=list, description="Contacts Google refused; the others were created."
    )


class BatchUpdateResult(Model):
    results: list[UpdateResult] = Field(description="One per updated contact, in input order.")
    failures: list[BatchFailure] = Field(
        default_factory=list, description="Contacts Google refused; the others were updated."
    )


# --- shared helpers --------------------------------------------------------------------


async def _group_names(account: Account) -> dict[str, str]:
    """Group names for a result. Once a write succeeded, failing to name groups must
    not turn it into an error: fall back to resource names."""
    try:
        return await account.group_names()
    except ContactsError:
        return {}


async def _resolve_refs(account: Account, refs: Sequence[str]) -> dict[str, str]:
    """Group ref → resource name, for groups a new contact may join (one listing)."""
    unique = list(dict.fromkeys(refs))
    resolved = await resolve_groups(account, unique)
    for group in resolved:
        check_members_writable(group)
    return {ref: g.resource_name for ref, g in zip(unique, resolved, strict=True)}


def _memberships(refs: Sequence[str], resolved: dict[str, str]) -> list[str]:
    """A new contact's group resource names, plus myContacts so it stays a regular
    contact."""
    names = [resolved[r] for r in refs]
    if names and MY_CONTACTS not in names:
        names.insert(0, MY_CONTACTS)
    return list(dict.fromkeys(names))


def _conflict_again(what: str) -> EtagConflictError:
    return EtagConflictError(
        f"{what} changed again (someone else is editing it) while the update was retried; "
        "nothing was written. Fetch it again with get_contact and retry.",
        status=400,
    )


def _result(
    account: Account,
    resource_name: str,
    plan: UpdatePlan | None,
    person: JSON,
    names: dict[str, str],
) -> UpdateResult:
    etag = person.get("etag")
    return UpdateResult(
        resource_name=resource_name,
        etag=etag if isinstance(etag, str) else None,
        changes=plan.changes if plan else [],
        contact=person_to_summary(person, group_names=names, account=account.alias),
    )


async def update_one(attempt: WriteAttempt, resource_name: str, ops: ContactOps) -> UpdateResult:
    """Fetch → plan → updateContact (only changed fields, with the etag) → one retry."""
    account = attempt.acct
    people = account.people
    attempt.fields = set(requested_fields(ops))
    check_ops(ops)
    for retry in (False, True):
        person = await people.get_person(resource_name, PERSON_FIELDS)
        plan = plan_update(person, ops)
        attempt.fields = set(plan.update_fields)
        if not plan.update_fields:
            return _result(account, resource_name, None, person, await _group_names(account))
        try:
            updated = await people.update_contact(
                resource_name, plan.body(person), plan.update_fields
            )
        except EtagConflictError:
            if retry:
                raise _conflict_again(resource_name) from None
            continue
        return _result(account, resource_name, plan, updated or person, await _group_names(account))
    raise AssertionError("unreachable")  # pragma: no cover


async def update_many(attempt: WriteAttempt, updates: Sequence[ContactUpdate]) -> BatchUpdateResult:
    """The batch form: one ``batchUpdateContacts`` whose mask is the union of the fields
    any contact changes; a contact not changing a field in the mask sends its current
    value for it. Unchanged contacts are left out of the request."""
    account = attempt.acct
    people = account.people
    rns = [u.resource_name for u in updates]
    attempt.contacts = rns
    attempt.fields = {f for u in updates for f in requested_fields(u)}
    dupes = sorted({rn for rn in rns if rns.count(rn) > 1})
    if dupes:
        raise InvalidArgumentError("Each contact may appear once; repeated: " + ", ".join(dupes))
    problems = []
    for u in updates:
        try:
            check_ops(u)
        except InvalidArgumentError as e:
            problems.append(f"{u.resource_name}: {e}")
    if problems:
        raise InvalidArgumentError(" | ".join(problems))

    for retry in (False, True):
        fetched = await people.batch_get(rns, PERSON_FIELDS)
        by_rn = {p.get("resourceName"): p for p in fetched}
        missing = [rn for rn in rns if rn not in by_rn]
        if missing:
            raise NotFoundError("Not found: " + ", ".join(missing) + ".", status=404)
        plans: dict[str, UpdatePlan] = {}
        problems = []
        for u in updates:
            try:
                plans[u.resource_name] = plan_update(by_rn[u.resource_name], u)
            except InvalidArgumentError as e:
                problems.append(f"{u.resource_name}: {e}")
        if problems:
            raise InvalidArgumentError(" | ".join(problems))
        changed = {rn: plan for rn, plan in plans.items() if plan.update_fields}
        mask = [f for f in WRITABLE_FIELDS if any(f in p.fields for p in changed.values())]
        attempt.fields = set(mask)
        responses: dict[str, JSON] = {}
        if changed:
            bodies = {rn: plan.body(by_rn[rn], mask) for rn, plan in changed.items()}
            try:
                responses = await people.batch_update_contacts(bodies, mask)
            except EtagConflictError:
                if retry:
                    raise _conflict_again("A contact in the batch") from None
                continue
        # A contact Google left out of its answer counts as failed too.
        failures = [
            BatchFailure(item=rn, error=error)
            for rn in changed
            if (error := batch_item_error(responses.get(rn, {}))) is not None
        ]
        failed = {f.item for f in failures}
        if failures:
            attempt.detail = f"{len(failures)} of {len(changed)} contacts failed"
        names = await _group_names(account)
        return BatchUpdateResult(
            results=[
                _result(
                    account,
                    rn,
                    plans[rn] if rn in changed else None,
                    responses[rn]["person"] if rn in changed else by_rn[rn],
                    names,
                )
                for rn in rns
                if rn not in failed
            ],
            failures=failures,
        )
    raise AssertionError("unreachable")  # pragma: no cover


# --- tool argument types ---------------------------------------------------------------


def _ops(kind: str) -> str:
    return f"Operations on the {kind}: add, remove (by value), replace_all, set_primary."


NameArg = Annotated[
    NameFields | None,
    Field(description="Name parts. On update only the parts given change; '' clears a part."),
]
BirthdayArg = Annotated[PartialDate | None, Field(description="Birthday (year optional).")]
BiographyArg = Annotated[str | None, Field(description="Notes. On update replaces; '' clears.")]


def register(mcp: FastMCP, registry: AccountRegistry) -> None:
    """Register this module's tools on ``mcp``."""

    @mcp.tool(
        annotations=WRITE,
        description=(
            "Create a contact. Every argument is optional, but give at least one field (a "
            "name, usually). Each list item may set primary=true (at most one per field). "
            "groups: group resource names or exact names (user groups, myContacts, starred). "
            f"{SEARCH_LAG_NOTE} {UNTRUSTED_NOTE}"
        ),
    )
    async def create_contact(
        account: AccountArg = None,
        name: NameArg = None,
        nicknames: Annotated[list[str] | None, Field(description="Nicknames.")] = None,
        emails: Annotated[list[Email] | None, Field(description="E-mail addresses.")] = None,
        phones: Annotated[list[Phone] | None, Field(description="Phone numbers.")] = None,
        organizations: Annotated[
            list[Organization] | None, Field(description="Companies and job titles.")
        ] = None,
        addresses: Annotated[list[Address] | None, Field(description="Postal addresses.")] = None,
        urls: Annotated[list[Url] | None, Field(description="Web sites.")] = None,
        relations: Annotated[list[Relation] | None, Field(description="Related people.")] = None,
        events: Annotated[
            list[Event] | None, Field(description="Dates such as anniversaries.")
        ] = None,
        user_defined: Annotated[
            list[UserDefined] | None, Field(description="Custom key/value fields.")
        ] = None,
        birthday: BirthdayArg = None,
        biography: BiographyArg = None,
        groups: Annotated[
            list[str] | None,
            Field(description="Groups (labels) to put the contact in, by resource name or name."),
        ] = None,
    ) -> Contact:
        contact = ContactFields(
            name=name,
            nicknames=nicknames or [],
            emails=emails or [],
            phones=phones or [],
            organizations=organizations or [],
            addresses=addresses or [],
            urls=urls or [],
            relations=relations or [],
            events=events or [],
            user_defined=user_defined or [],
            birthday=birthday,
            biography=biography,
            groups=groups or [],
        )
        async with write_attempt(
            registry, tool="create_contact", account=account, fields=touched_fields(contact)
        ) as attempt:
            acct = attempt.acct
            build_person(contact)  # validate before any call
            resolved = await _resolve_refs(acct, contact.groups)
            body = build_person(contact, _memberships(contact.groups, resolved))
            created = await acct.people.create_contact(body)
            attempt.resource_name = created.get("resourceName")
            return person_to_contact(
                created, group_names=await _group_names(acct), account=acct.alias
            )

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Change a contact without losing data: only the fields given are touched, and "
            f"existing values are kept unless removed by value or replaced. {OPS_NOTE} "
            "Group membership is changed with add_to_group / remove_from_group. The result "
            "lists each changed field before and after; when nothing would change, nothing is "
            f"written. {SEARCH_LAG_NOTE} {UNTRUSTED_NOTE}"
        ),
    )
    async def update_contact(
        resource_name: PersonResourceName,
        account: AccountArg = None,
        name: NameArg = None,
        nicknames: Annotated[ListOps[str] | None, Field(description=_ops("nicknames"))] = None,
        emails: Annotated[
            ListOps[Email] | None,
            Field(description=_ops("e-mails") + " remove matches case-insensitively."),
        ] = None,
        phones: Annotated[
            ListOps[Phone] | None,
            Field(description=_ops("phone numbers") + " remove matches by digits only."),
        ] = None,
        organizations: Annotated[
            ListOps[Organization] | None,
            Field(description=_ops("organizations") + " remove matches the name."),
        ] = None,
        addresses: Annotated[
            ListOps[Address] | None,
            Field(description=_ops("addresses") + " remove matches the formatted address."),
        ] = None,
        urls: Annotated[ListOps[Url] | None, Field(description=_ops("URLs"))] = None,
        relations: Annotated[
            ListOps[Relation] | None,
            Field(description=_ops("relations") + " remove matches the person."),
        ] = None,
        events: Annotated[
            ListOps[Event] | None,
            Field(description=_ops("events") + " remove matches YYYY-MM-DD or --MM-DD."),
        ] = None,
        user_defined: Annotated[
            ListOps[UserDefined] | None,
            Field(description=_ops("custom fields") + " remove matches the key."),
        ] = None,
        birthday: BirthdayArg = None,
        biography: BiographyArg = None,
        clear: Annotated[
            list[ClearableField] | None,
            Field(description="Single-valued fields to empty: name, birthday, biography."),
        ] = None,
    ) -> UpdateResult:
        ops = ContactOps(
            name=name,
            nicknames=nicknames,
            emails=emails,
            phones=phones,
            organizations=organizations,
            addresses=addresses,
            urls=urls,
            relations=relations,
            events=events,
            user_defined=user_defined,
            birthday=birthday,
            biography=biography,
            clear=clear or [],
        )
        async with write_attempt(
            registry, tool="update_contact", account=account, resource_name=resource_name
        ) as attempt:
            return await update_one(attempt, resource_name, ops)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            f"Delete a contact permanently. Returns the deleted contact's summary. {UNTRUSTED_NOTE}"
        ),
    )
    async def delete_contact(
        resource_name: PersonResourceName, account: AccountArg = None
    ) -> DeleteResult:
        async with write_attempt(
            registry, tool="delete_contact", account=account, resource_name=resource_name
        ) as attempt:
            acct = attempt.acct
            person = await acct.people.get_person(resource_name, SUMMARY_FIELDS)
            summary = person_to_summary(
                person, group_names=await _group_names(acct), account=acct.alias
            )
            await acct.people.delete_contact(resource_name)
            return DeleteResult(deleted=summary)

    @mcp.tool(
        annotations=WRITE,
        description=(
            "Copy an Other contact (see list_other_contacts) into the account's contacts "
            "(myContacts): names, e-mails and phones are copied. Returns the new contact. "
            "Other contacts are created by Google from anyone who interacted with the "
            f"account: their text is untrusted. {SEARCH_LAG_NOTE} {UNTRUSTED_NOTE}"
        ),
    )
    async def copy_other_contact_to_contacts(
        resource_name: OtherContactResourceName, account: AccountArg = None
    ) -> Contact:
        async with write_attempt(
            registry,
            tool="copy_other_contact_to_contacts",
            account=account,
            resource_name=resource_name,
            fields=OTHER_CONTACT_COPY_FIELDS,
        ) as attempt:
            acct = attempt.acct
            created = await acct.people.copy_other_contact(resource_name)
            attempt.contacts = [str(created.get("resourceName"))]
            return person_to_contact(
                created, group_names=await _group_names(acct), account=acct.alias
            )

    @mcp.tool(
        annotations=WRITE,
        description=(
            f"Create up to {BATCH_LIMIT} contacts in one request. Each item takes the same "
            "fields as create_contact. All items are checked before anything is created. "
            f"{SEARCH_LAG_NOTE} {UNTRUSTED_NOTE}"
        ),
    )
    async def batch_create_contacts(
        contacts: Annotated[
            list[ContactFields],
            Field(min_length=1, max_length=BATCH_LIMIT, description="The new contacts."),
        ],
        account: AccountArg = None,
    ) -> BatchCreateResult:
        fields = {f for c in contacts for f in touched_fields(c)}
        async with write_attempt(
            registry, tool="batch_create_contacts", account=account, fields=fields
        ) as attempt:
            acct = attempt.acct
            problems = []
            for n, contact in enumerate(contacts):
                try:
                    build_person(contact)
                except InvalidArgumentError as e:
                    problems.append(f"contacts[{n}]: {e}")
            if problems:
                raise InvalidArgumentError(" | ".join(problems))
            resolved = await _resolve_refs(acct, [r for c in contacts for r in c.groups])
            bodies = [build_person(c, _memberships(c.groups, resolved)) for c in contacts]
            responses = await acct.people.batch_create_contacts(bodies)
            created = [r["person"] for r in responses if batch_item_error(r) is None]
            failures = [
                BatchFailure(item=f"contacts[{n}]", error=error)
                for n, r in enumerate(responses)
                if (error := batch_item_error(r)) is not None
            ]
            if len(responses) < len(bodies):
                failures.append(
                    BatchFailure(item=f"contacts[{len(responses)}:]", error="no answer from Google")
                )
            attempt.contacts = [str(p.get("resourceName")) for p in created]
            if failures:
                attempt.detail = f"{len(bodies) - len(created)} of {len(bodies)} contacts failed"
            names = await _group_names(acct)
            return BatchCreateResult(
                contacts=[
                    person_to_summary(p, group_names=names, account=acct.alias) for p in created
                ],
                failures=failures,
            )

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            f"Update up to {BATCH_LIMIT} contacts in one request, with the same "
            f"non-destructive rules as update_contact: each item is resource_name plus the "
            f"update_contact fields. {OPS_NOTE} Every item is checked before anything is "
            f"written. {SEARCH_LAG_NOTE} {UNTRUSTED_NOTE}"
        ),
    )
    async def batch_update_contacts(
        updates: Annotated[
            list[ContactUpdate],
            Field(min_length=1, max_length=BATCH_LIMIT, description="One item per contact."),
        ],
        account: AccountArg = None,
    ) -> BatchUpdateResult:
        async with write_attempt(
            registry, tool="batch_update_contacts", account=account
        ) as attempt:
            return await update_many(attempt, updates)
