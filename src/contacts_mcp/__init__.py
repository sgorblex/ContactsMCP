"""ContactsMCP: a self-hostable MCP server for Google Contacts, with multiple accounts."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("contacts-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"
