"""MCP tools. Each module has ``register(mcp, registry)``."""

from __future__ import annotations

from fastmcp import FastMCP

from contacts_mcp.registry import AccountRegistry
from contacts_mcp.tools import groups, other, read, write


def register_all(mcp: FastMCP, registry: AccountRegistry) -> None:
    read.register(mcp, registry)
    other.register(mcp, registry)
    write.register(mcp, registry)
    groups.register(mcp, registry)
