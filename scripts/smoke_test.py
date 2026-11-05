"""Manual smoke test against a REAL Google account. Never run in CI.

It creates one uniquely named contact and one label (contact group) in the account, exercises
the tools end to end, and deletes both again, also when a step fails.

    CONTACTS_MCP_SMOKE=1 mise exec -- uv run python scripts/smoke_test.py [alias]

Configuration is the server's own: ``GOOGLE_CLIENT_*`` / ``GOOGLE_CLIENT_SECRETS_FILE`` for the
OAuth client and ``CONTACTS_MCP_DATA_DIR`` for the linked accounts (link one first with
``contacts-mcp auth add <alias>``). The alias is the first argument, or
``CONTACTS_MCP_SMOKE_ACCOUNT``, or the only linked account. Use a throwaway account if you can:
the script only touches what it creates, but it does write.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from typing import Any

import httpx
from fastmcp import Client, FastMCP

from contacts_mcp.accounts import AccountFileStore
from contacts_mcp.config import load_data_dir, load_google_client
from contacts_mcp.registry import AccountRegistry
from contacts_mcp.tools import register_all

SEARCH_ATTEMPTS = 8
SEARCH_FIRST_DELAY = 2.0
SEARCH_MAX_DELAY = 20.0

FIRST_PHONE = "+39 06 5550 0101"
SECOND_PHONE = "+39 06 5550 0202"


class SmokeError(Exception):
    """A check of the smoke test failed."""


def step(message: str) -> None:
    print(f"==> {message}", flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)
    print(f"    ok: {message}", flush=True)


def digits(value: str) -> str:
    return re.sub(r"\D", "", value)


async def call(client: Client[Any], tool: str, /, **arguments: Any) -> dict[str, Any]:
    """Call a tool and return its structured content (raises on a tool error)."""
    result = await client.call_tool(tool, arguments)
    content = result.structured_content
    if not isinstance(content, dict):
        raise SmokeError(f"{tool}: no structured content in the result")
    return content


def phone_digits(contact: dict[str, Any]) -> list[str]:
    return [digits(str(p["value"])) for p in contact.get("phones", [])]


def pick_account(registry: AccountRegistry, requested: str | None) -> str:
    aliases = registry.aliases()
    if not aliases:
        raise SmokeError("no linked account: run `contacts-mcp auth add <alias>` first")
    if requested is not None:
        if requested not in aliases:
            raise SmokeError(f"unknown account {requested!r}; linked: {', '.join(aliases)}")
        return requested
    if len(aliases) > 1:
        raise SmokeError(f"several accounts linked ({', '.join(aliases)}): name one")
    return aliases[0]


async def search_with_retries(client: Client[Any], account: str, token: str) -> bool:
    """People API search lags behind writes: poll, bounded."""
    delay = SEARCH_FIRST_DELAY
    for attempt in range(1, SEARCH_ATTEMPTS + 1):
        found = await call(client, "search_contacts", query=token, account=account, limit=10)
        if found.get("contacts"):
            print(f"    found after {attempt} attempt(s)", flush=True)
            return True
        print(f"    not indexed yet (attempt {attempt}/{SEARCH_ATTEMPTS})", flush=True)
        await asyncio.sleep(delay)
        delay = min(delay * 2, SEARCH_MAX_DELAY)
    return False


async def run_checks(
    client: Client[Any], account: str, token: str, created: dict[str, str]
) -> None:
    """The scenario. ``created`` collects resource names for cleanup as soon as they exist."""
    step("accounts")
    accounts = await call(client, "accounts")
    check(any(a.get("alias") == account for a in accounts.get("accounts", [])), "account listed")

    step("create_contact")
    contact = await call(
        client,
        "create_contact",
        account=account,
        name={"given": "ContactsMCP", "family": token},
        phones=[{"value": FIRST_PHONE, "type": "mobile"}],
        emails=[{"value": f"{token.lower()}@example.com", "type": "work"}],
    )
    resource_name = str(contact["resource_name"])
    created["contact"] = resource_name
    check(resource_name.startswith("people/"), f"created {resource_name}")

    step("get_contact")
    fetched = await call(client, "get_contact", resource_name=resource_name, account=account)
    check(fetched.get("etag") is not None, "etag present")
    check(digits(FIRST_PHONE) in phone_digits(fetched), "first phone stored")

    step("update_contact: add a second phone, keep the first")
    await call(
        client,
        "update_contact",
        resource_name=resource_name,
        account=account,
        phones={"add": [{"value": SECOND_PHONE, "type": "work"}]},
    )
    after = await call(client, "get_contact", resource_name=resource_name, account=account)
    numbers = phone_digits(after)
    check(digits(FIRST_PHONE) in numbers, "first phone still there")
    check(digits(SECOND_PHONE) in numbers, "second phone added")
    check(len(numbers) == 2, "exactly two phones")

    step("update_contact: set a custom field twice, one value remains")
    for value in ("first", "second"):
        await call(
            client,
            "update_contact",
            resource_name=resource_name,
            account=account,
            user_defined={"set": [{"key": "smoke", "value": value}]},
        )
    after = await call(client, "get_contact", resource_name=resource_name, account=account)
    check(after.get("user_defined") == [{"key": "smoke", "value": "second"}], "value replaced")

    step("create_group + add_to_group")
    group = await call(client, "create_group", account=account, name=f"smoke-{token}")
    group_name = str(group["resource_name"])
    created["group"] = group_name
    check(group_name.startswith("contactGroups/"), f"created {group_name}")
    await call(
        client,
        "add_to_group",
        account=account,
        group=group_name,
        resource_names=[resource_name],
    )
    member = await call(client, "get_contact", resource_name=resource_name, account=account)
    check(
        any(g.get("resource_name") == group_name for g in member.get("groups", [])),
        "contact is a member of the label",
    )

    step("search_contacts (the index may lag behind writes)")
    if not await search_with_retries(client, account, token):
        print("    WARNING: not found by search within the retry budget (index lag?)")


async def cleanup(client: Client[Any], account: str, created: dict[str, str]) -> list[str]:
    """Delete what was created; returns the failures instead of raising."""
    failures: list[str] = []
    if "contact" in created:
        step(f"delete_contact {created['contact']}")
        try:
            await call(client, "delete_contact", resource_name=created["contact"], account=account)
        except Exception as e:
            failures.append(f"contact {created['contact']}: {e}")
    if "group" in created:
        step(f"delete_group {created['group']}")
        try:
            await call(client, "delete_group", account=account, group=created["group"])
        except Exception as e:
            failures.append(f"label {created['group']}: {e}")
    return failures


async def main() -> int:
    if os.environ.get("CONTACTS_MCP_SMOKE") != "1":
        print(
            "This talks to a real Google account and creates/deletes a contact and a label.\n"
            "Set CONTACTS_MCP_SMOKE=1 to run it.",
            file=sys.stderr,
        )
        return 2

    requested = (sys.argv[1] if len(sys.argv) > 1 else None) or os.environ.get(
        "CONTACTS_MCP_SMOKE_ACCOUNT"
    )
    token = f"Smoke{uuid.uuid4().hex[:8]}"
    created: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=30.0) as http:
        registry = AccountRegistry(AccountFileStore(load_data_dir()), load_google_client(), http)
        registry.reload()
        account = pick_account(registry, requested)
        mcp: FastMCP = FastMCP("contacts-smoke")
        register_all(mcp, registry)

        error: Exception | None = None
        async with Client(mcp) as client:
            try:
                await run_checks(client, account, token, created)
            except Exception as e:
                error = e
            leftovers = await cleanup(client, account, created)

    for leftover in leftovers:
        print(f"CLEANUP FAILED, delete by hand: {leftover}", file=sys.stderr)
    if error is not None:
        print(f"FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if leftovers:
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
