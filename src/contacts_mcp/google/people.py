"""A small async People API client over httpx.

Methods return the API's JSON (``dict``) unchanged. Every request:

- carries the account's access token, refreshed once on a 401;
- is retried with exponential backoff and jitter, a bounded number of times and
  honouring ``Retry-After``: reads on 429, 5xx and transport errors, writes only when
  Google certainly did not act on them (429, or no connection at all);
- maps failures to :mod:`contacts_mcp.errors` (``NotFoundError``,
  ``EtagConflictError``, ``PermissionDeniedError``, ``RateLimitedError``,
  ``GoogleApiError``) with short messages, never the raw body or a token.

Callers validate resource names before calling, and serialize the writes of an account.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from contacts_mcp.errors import (
    EtagConflictError,
    GoogleApiError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)
from contacts_mcp.fields import (
    OTHER_CONTACT_COPY_FIELDS,
    OTHER_CONTACT_FIELDS,
    OTHER_CONTACT_SEARCH_FIELDS,
    PEOPLE_API_URL,
    PERSON_FIELDS,
    SUMMARY_FIELDS,
    mask,
)
from contacts_mcp.google.oauth import TokenProvider
from contacts_mcp.sanitize import clean

JSON = dict[str, Any]

log = logging.getLogger(__name__)

BATCH_LIMIT = 200
GROUP_FIELDS = "name,groupType,memberCount,metadata"


class PeopleClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenProvider,
        *,
        base_url: str = PEOPLE_API_URL,
        max_retries: int = 4,
        max_backoff: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._http = http
        self.tokens = tokens
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries
        self._max_backoff = max_backoff
        self._sleep = sleep or asyncio.sleep

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: JSON | None = None,
    ) -> JSON:
        """``{base_url}/{path}``; returns the JSON body (``{}`` when empty)."""
        url = f"{self._base}/{path}"
        # A write that failed with a 5xx or a read timeout may have been applied anyway:
        # repeating it could create a second contact or report a done delete as not found.
        # Writes are retried only when Google certainly did not act on them (429, or no
        # connection at all).
        safe = method.upper() == "GET"
        refreshed = False
        attempt = 0
        while True:
            token = await self.tokens.access_token()
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TransportError as e:
                not_sent = isinstance(
                    e, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout
                )
                if (safe or not_sent) and attempt < self._max_retries:
                    await self._sleep(self._backoff(attempt, None))
                    attempt += 1
                    continue
                if not safe and not not_sent:
                    raise GoogleApiError(
                        f"The People API did not answer ({type(e).__name__}); the change may "
                        "or may not have been applied. Check before trying again."
                    ) from None
                raise GoogleApiError(
                    f"Could not reach the People API ({type(e).__name__})."
                ) from None

            status = response.status_code
            if status == 401 and not refreshed:
                refreshed = True
                await self.tokens.access_token(force_refresh=True)
                continue
            retryable = status == 429 or (status >= 500 and safe)
            if retryable and attempt < self._max_retries:
                delay = self._backoff(attempt, response.headers.get("Retry-After"))
                log.info(
                    "people api retry",
                    extra={"status": status, "attempt": attempt + 1, "delay": round(delay, 2)},
                )
                await self._sleep(delay)
                attempt += 1
                continue
            if status >= 500 and not safe:
                raise GoogleApiError(
                    f"The People API failed (HTTP {status}); the change may or may not have "
                    "been applied. Check before trying again.",
                    status=status,
                )
            if status >= 400:
                raise _map_error(response, path)
            if not response.content:
                return {}
            try:
                body = response.json()
            except ValueError:
                raise GoogleApiError("The People API returned an unreadable response.") from None
            return body if isinstance(body, dict) else {}

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            seconds = _parse_retry_after(retry_after)
            if seconds is not None:
                return min(self._max_backoff, max(0.0, seconds))
        base = min(self._max_backoff, 0.5 * 2**attempt)
        return random.uniform(base / 2, base)  # noqa: S311 - jitter, not crypto

    # --- people ------------------------------------------------------------------------

    async def get_me(self) -> JSON:
        """``people/me`` with ``emailAddresses`` (used by ``auth add`` to learn the e-mail)."""
        return await self.request("GET", "people/me", params={"personFields": "emailAddresses"})

    async def get_person(
        self, resource_name: str, person_fields: tuple[str, ...] = PERSON_FIELDS
    ) -> JSON:
        return await self.request(
            "GET", resource_name, params={"personFields": mask(person_fields)}
        )

    async def batch_get(
        self, resource_names: list[str], person_fields: tuple[str, ...] = SUMMARY_FIELDS
    ) -> list[JSON]:
        """``people:batchGet`` (max 200 per call; chunked here). Returns found persons only."""
        found: list[JSON] = []
        for i in range(0, len(resource_names), BATCH_LIMIT):
            chunk = resource_names[i : i + BATCH_LIMIT]
            params = [("resourceNames", rn) for rn in chunk]
            params.append(("personFields", mask(person_fields)))
            body = await self.request("GET", "people:batchGet", params=params)
            for item in body.get("responses", []):
                person = item.get("person")
                if isinstance(person, dict):
                    found.append(person)
        return found

    async def list_connections(
        self,
        *,
        page_size: int,
        page_token: str | None = None,
        sort_order: str | None = None,
        person_fields: tuple[str, ...] = SUMMARY_FIELDS,
    ) -> JSON:
        """``people/me/connections``: ``{connections, nextPageToken, totalItems}``."""
        params: dict[str, Any] = {"personFields": mask(person_fields), "pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        if sort_order:
            params["sortOrder"] = sort_order
        return await self.request("GET", "people/me/connections", params=params)

    async def search_contacts(
        self, query: str, *, page_size: int, read_mask: tuple[str, ...] = SUMMARY_FIELDS
    ) -> JSON:
        """``people:searchContacts``: ``{results: [{person}]}``. Warm-up is the caller's job."""
        return await self.request(
            "GET",
            "people:searchContacts",
            params={"query": query, "pageSize": page_size, "readMask": mask(read_mask)},
        )

    async def create_contact(
        self, person: JSON, *, person_fields: tuple[str, ...] = PERSON_FIELDS
    ) -> JSON:
        return await self.request(
            "POST",
            "people:createContact",
            params={"personFields": mask(person_fields)},
            json=person,
        )

    async def update_contact(
        self,
        resource_name: str,
        person: JSON,
        update_person_fields: list[str],
        *,
        person_fields: tuple[str, ...] = PERSON_FIELDS,
    ) -> JSON:
        """``person`` must carry the ``etag`` it was read with; a stale one raises
        ``EtagConflictError``."""
        return await self.request(
            "PATCH",
            f"{resource_name}:updateContact",
            params={
                "updatePersonFields": ",".join(update_person_fields),
                "personFields": mask(person_fields),
            },
            json=person,
        )

    async def delete_contact(self, resource_name: str) -> None:
        await self.request("DELETE", f"{resource_name}:deleteContact")

    async def batch_create_contacts(
        self, persons: list[JSON], *, person_fields: tuple[str, ...] = PERSON_FIELDS
    ) -> list[JSON]:
        """``people:batchCreateContacts`` (max 200).

        Returns Google's per-contact responses in input order: each holds the created
        ``person``, or a ``status`` when that contact failed (see :func:`batch_item_error`).
        """
        if len(persons) > BATCH_LIMIT:
            raise GoogleApiError(f"At most {BATCH_LIMIT} contacts per batch.")
        body = await self.request(
            "POST",
            "people:batchCreateContacts",
            json={
                "contacts": [{"contactPerson": p} for p in persons],
                "readMask": mask(person_fields),
            },
        )
        return [r for r in body.get("createdPeople", []) if isinstance(r, dict)]

    async def batch_update_contacts(
        self,
        persons: dict[str, JSON],
        update_mask: list[str],
        *,
        person_fields: tuple[str, ...] = PERSON_FIELDS,
    ) -> dict[str, JSON]:
        """``people:batchUpdateContacts`` (max 200).

        Returns resourceName → Google's response for that contact: the updated ``person``,
        or a ``status`` when that contact failed (see :func:`batch_item_error`).
        """
        if len(persons) > BATCH_LIMIT:
            raise GoogleApiError(f"At most {BATCH_LIMIT} contacts per batch.")
        body = await self.request(
            "POST",
            "people:batchUpdateContacts",
            json={
                "contacts": persons,
                "updateMask": ",".join(update_mask),
                "readMask": mask(person_fields),
            },
        )
        results = body.get("updateResult", {})
        return {rn: r for rn, r in results.items() if isinstance(r, dict)}

    # --- contact groups ----------------------------------------------------------------

    async def list_groups(self) -> list[JSON]:
        """Every contact group (all pages), with ``groupFields=name,groupType,memberCount``."""
        groups: list[JSON] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": 1000, "groupFields": GROUP_FIELDS}
            if page_token:
                params["pageToken"] = page_token
            body = await self.request("GET", "contactGroups", params=params)
            groups.extend(body.get("contactGroups", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                return groups

    async def get_group(self, resource_name: str, *, max_members: int = 0) -> JSON:
        return await self.request(
            "GET",
            resource_name,
            params={"maxMembers": max_members, "groupFields": GROUP_FIELDS},
        )

    async def create_group(self, name: str) -> JSON:
        return await self.request(
            "POST",
            "contactGroups",
            json={"contactGroup": {"name": name}, "readGroupFields": GROUP_FIELDS},
        )

    async def update_group(self, resource_name: str, name: str, etag: str) -> JSON:
        return await self.request(
            "PUT",
            resource_name,
            json={
                "contactGroup": {"name": name, "etag": etag},
                "updateGroupFields": "name",
                "readGroupFields": GROUP_FIELDS,
            },
        )

    async def delete_group(self, resource_name: str, *, delete_contacts: bool) -> None:
        await self.request(
            "DELETE",
            resource_name,
            params={"deleteContacts": "true" if delete_contacts else "false"},
        )

    async def modify_group_members(
        self,
        resource_name: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> JSON:
        """``contactGroups/{id}/members:modify``: ``{notFoundResourceNames,
        canNotRemoveLastContactGroupResourceNames}``."""
        body: JSON = {}
        if add:
            body["resourceNamesToAdd"] = add
        if remove:
            body["resourceNamesToRemove"] = remove
        return await self.request("POST", f"{resource_name}/members:modify", json=body)

    # --- other contacts ----------------------------------------------------------------

    async def list_other_contacts(
        self,
        *,
        page_size: int,
        page_token: str | None = None,
        read_mask: tuple[str, ...] = OTHER_CONTACT_FIELDS,
    ) -> JSON:
        """``otherContacts``: ``{otherContacts, nextPageToken, totalSize}``."""
        params: dict[str, Any] = {"readMask": mask(read_mask), "pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        return await self.request("GET", "otherContacts", params=params)

    async def search_other_contacts(
        self,
        query: str,
        *,
        page_size: int,
        read_mask: tuple[str, ...] = OTHER_CONTACT_SEARCH_FIELDS,
    ) -> JSON:
        return await self.request(
            "GET",
            "otherContacts:search",
            params={"query": query, "pageSize": page_size, "readMask": mask(read_mask)},
        )

    async def copy_other_contact(
        self, resource_name: str, *, copy_mask: tuple[str, ...] = OTHER_CONTACT_COPY_FIELDS
    ) -> JSON:
        """``otherContacts/{id}:copyOtherContactToMyContactsGroup``: the new person."""
        return await self.request(
            "POST",
            f"{resource_name}:copyOtherContactToMyContactsGroup",
            json={"copyMask": mask(copy_mask), "readMask": mask(PERSON_FIELDS)},
        )


def batch_item_error(item: JSON) -> str | None:
    """Why one contact of a batch write failed (short and cleaned), or None if it worked."""
    if isinstance(item.get("person"), dict):
        return None
    status = item.get("status")
    message = status.get("message") if isinstance(status, dict) else None
    code = item.get("httpStatusCode")
    head = f"HTTP {code}" if isinstance(code, int) else "failed"
    return f"{head}: {clean(message)[:200]}" if isinstance(message, str) and message else head


def _parse_retry_after(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (when - datetime.now(UTC)).total_seconds()


def _map_error(response: httpx.Response, path: str) -> GoogleApiError:
    """A short, actionable error for a People API failure. Never the raw body."""
    status = response.status_code
    reason: str | None = None
    api_status: str | None = None
    message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        api_status = err.get("status") if isinstance(err.get("status"), str) else None
        reason = api_status
        if isinstance(err.get("message"), str):
            message = clean(err["message"])[:200]
        for detail in err.get("details") or []:
            if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
                reason = detail["reason"]
                break

    target = path.split(":", 1)[0]
    if status == 404:
        return NotFoundError(f"Not found: {target}.", status=status, reason=reason)
    # Google: 400 with status FAILED_PRECONDITION (reason failedPrecondition) when the
    # etag sent is not the contact's current one. Other 400s mentioning "etag" (e.g. a
    # malformed one) are not conflicts and must not trigger the re-fetch-and-retry.
    precondition = api_status == "FAILED_PRECONDITION" or (reason or "").lower() in (
        "failedprecondition",
        "failed_precondition",
    )
    if status == 400 and precondition and "etag" in message.lower():
        return EtagConflictError(
            f"{target} was changed by someone else since it was read.",
            status=status,
            reason=reason,
        )
    if status == 403:
        if reason == "SERVICE_DISABLED":
            text = (
                "The People API is not enabled in the Google Cloud project of the OAuth "
                "client. Enable it in the Cloud console (APIs & Services → Library)."
            )
        elif reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT":
            text = (
                "The account was linked without the needed scope. Re-link it with "
                "`contacts-mcp auth add <alias>` and grant every permission asked."
            )
        else:
            text = (
                "Google refused access (403). For a Google Workspace account, an admin may "
                "have to allow this OAuth client (Admin console → Security → API controls)."
            )
        return PermissionDeniedError(text, status=status, reason=reason)
    if status == 429 or status >= 500:
        return RateLimitedError(
            f"The People API is rate-limiting or failing (HTTP {status}); try again later.",
            status=status,
            reason=reason,
        )
    if status == 401:
        return GoogleApiError(
            "Google rejected the access token even after a refresh.", status=status, reason=reason
        )
    detail = f": {message}" if message else ""
    return GoogleApiError(f"People API error (HTTP {status}){detail}", status=status, reason=reason)
