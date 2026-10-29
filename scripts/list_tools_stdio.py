"""Spawn ``contacts-mcp`` over stdio and print the tools it exposes, with their annotations.

    mise exec -- uv run python scripts/list_tools_stdio.py

Needs a Google OAuth client in the environment (``GOOGLE_CLIENT_ID`` and
``GOOGLE_CLIENT_SECRET``, or ``GOOGLE_CLIENT_SECRETS_FILE``): the server refuses to start
without one. The environment is passed through to the child process. Any other ``CONTACTS_MCP_*``
setting (for instance ``CONTACTS_MCP_EXPOSED_TOOLS=read-only``) applies too.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def command() -> tuple[str, list[str]]:
    """The installed ``contacts-mcp`` if on PATH, else the current interpreter's module."""
    exe = shutil.which("contacts-mcp")
    if exe is not None:
        return exe, []
    return sys.executable, ["-m", "contacts_mcp"]


async def main() -> int:
    env = {**os.environ, "CONTACTS_MCP_TRANSPORT": "stdio"}
    program, args = command()
    transport = StdioTransport(command=program, args=args, env=env, keep_alive=False)
    async with Client(transport) as client:
        tools = await client.list_tools()

    print(f"{len(tools)} tools")
    for tool in sorted(tools, key=lambda t: t.name):
        annotations = tool.annotations
        flags = []
        for hint in HINTS:
            attr = "".join(f"_{c.lower()}" if c.isupper() else c for c in hint)  # snake_case
            value = getattr(annotations, attr, None) if annotations is not None else None
            if value is not None:
                flags.append(f"{hint}={str(value).lower()}")
        print(f"{tool.name:34} {' '.join(flags) or '(no annotations)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
