"""``CONTACTS_MCP_EXPOSED_TOOLS``: which tools the server lists and serves.

- ``all`` (default): every tool.
- ``read-only``: only tools annotated ``readOnlyHint=True``.
- ``read-only+tool,tool``: those plus the named write tools.
- A trailing ``-tool,tool`` hides named tools from either base, e.g.
  ``all-delete_contact,delete_group`` or ``read-only+create_contact-list_other_contacts``.

Tool names use underscores, so a ``-`` after the base always starts the exclusion
list. Unknown names stop the server at startup: a typo must not silently produce a
different surface that looks like it worked. Removed tools are deleted from the
server, so they are neither listed nor callable (a call is an unknown tool).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Sequence
from dataclasses import dataclass, field

from fastmcp import FastMCP
from fastmcp.tools import Tool

from contacts_mcp.config import ConfigError

ENV = "CONTACTS_MCP_EXPOSED_TOOLS"
MODES = ("all", "read-only")
ALLOW_SEPARATOR = "+"
EXCLUDE_SEPARATOR = "-"
_HYPHEN_HINT = f" Tool names use underscores; '{EXCLUDE_SEPARATOR}' starts the exclusion list."


@dataclass(frozen=True)
class ExposureMode:
    """A validated ``CONTACTS_MCP_EXPOSED_TOOLS`` value."""

    base: str
    allowlist: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()

    def __str__(self) -> str:
        value = self.base
        if self.allowlist:
            value += ALLOW_SEPARATOR + ",".join(self.allowlist)
        if self.exclusions:
            value += EXCLUDE_SEPARATOR + ",".join(self.exclusions)
        return value


@dataclass
class ExposureResult:
    """Tools removed by the base mode (``hidden``) and by the ``-`` list (``excluded``)."""

    hidden: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)


def _split_list(raw: str | None) -> list[str] | None:
    """A comma-separated tool list; ``None`` means its separator was absent."""
    if raw is None:
        return None
    return [name.strip() for name in raw.split(",") if name.strip()]


def _split(value: str) -> tuple[str | None, list[str] | None, list[str] | None]:
    """Split a lowercased value into base mode, ``+`` allowlist and ``-`` exclusions.

    The base is matched as a known-mode prefix rather than by splitting on ``-``,
    because ``read-only`` itself contains one.
    """
    for base in sorted(MODES, key=len, reverse=True):
        rest = value[len(base) :]
        if value.startswith(base) and rest[:1] in ("", ALLOW_SEPARATOR, EXCLUDE_SEPARATOR):
            break
    else:
        return None, None, None

    raw_allow: str | None = None
    raw_exclude: str | None = None
    if rest.startswith(ALLOW_SEPARATOR):
        raw_allow, separator, tail = rest[1:].partition(EXCLUDE_SEPARATOR)
        if separator:
            raw_exclude = tail
    elif rest.startswith(EXCLUDE_SEPARATOR):
        raw_exclude = rest[1:]
    return base, _split_list(raw_allow), _split_list(raw_exclude)


def parse_exposed_tools(value: str) -> ExposureMode:
    """Parse and validate the syntax of ``value``; names are checked by :func:`apply_exposure`."""
    raw = value
    base, allowlist, exclusions = _split(value.strip().lower())
    invalid = f"Invalid {ENV} {raw!r}."
    if base is None:
        raise ConfigError(f"{invalid} Expected one of: {', '.join(MODES)}.")
    if allowlist is not None and base != "read-only":
        raise ConfigError(
            f"{invalid} The '{ALLOW_SEPARATOR}tool,tool' allowlist is only valid with read-only."
        )
    if allowlist is not None and not allowlist:
        raise ConfigError(
            f"{invalid} The '{ALLOW_SEPARATOR}' allowlist must name at least one tool."
        )
    if exclusions is not None:
        if not exclusions:
            raise ConfigError(
                f"{invalid} The '{EXCLUDE_SEPARATOR}' exclusion list must name at least one tool."
            )
        if any(ALLOW_SEPARATOR in n or EXCLUDE_SEPARATOR in n for n in exclusions):
            raise ConfigError(
                f"{invalid} Use one '{ALLOW_SEPARATOR}' list followed by one "
                f"'{EXCLUDE_SEPARATOR}' list, each comma-separated."
            )
        conflicting = sorted(set(allowlist or ()) & set(exclusions))
        if conflicting:
            raise ConfigError(
                f"{invalid} Tool(s) both exposed with '{ALLOW_SEPARATOR}' and excluded with "
                f"'{EXCLUDE_SEPARATOR}': {', '.join(conflicting)}."
            )
    return ExposureMode(base, tuple(allowlist or ()), tuple(exclusions or ()))


def is_read_only(tool: Tool) -> bool:
    """Whether ``tool`` is annotated ``readOnlyHint=True`` (missing means a write tool)."""
    return bool(tool.annotations is not None and tool.annotations.read_only_hint)


def registered_tools(mcp: FastMCP) -> list[Tool]:
    """Every tool registered on ``mcp`` (its local provider), callable outside a loop too.

    fastmcp only lists tools asynchronously; this runs the listing on a private event
    loop in a worker thread so that it works whether or not the caller is itself
    running inside an event loop (tests build servers from async code).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        tools: Sequence[Tool] = pool.submit(asyncio.run, mcp.local_provider.list_tools()).result()
    return list(tools)


def _reject_unknown(kind: str, names: Sequence[str], registered: set[str], hint: str = "") -> None:
    unknown = sorted(set(names) - registered)
    if unknown:
        raise ConfigError(f"Invalid {ENV} {kind}: unknown tool(s) {', '.join(unknown)}.{hint}")


def apply_exposure(mcp: FastMCP, value: str) -> ExposureResult:
    """Remove from ``mcp`` the tools ``value`` does not expose. Raises ConfigError."""
    mode = parse_exposed_tools(value)
    if mode.base == "all" and not mode.exclusions:
        return ExposureResult()

    tools = registered_tools(mcp)
    registered = {tool.name for tool in tools}
    # "read-only+create-contact" splits into "+create" and "-contact"; point at the
    # hyphen when a "-" list could be the result of one.
    hint = _HYPHEN_HINT if mode.exclusions else ""
    _reject_unknown("allowlist", mode.allowlist, registered, hint)
    _reject_unknown("exclusion list", mode.exclusions, registered)

    allowed = set(mode.allowlist)
    excluded = set(mode.exclusions)

    def base_exposes(tool: Tool) -> bool:
        return mode.base == "all" or tool.name in allowed or is_read_only(tool)

    already_hidden = sorted(t.name for t in tools if t.name in excluded and not base_exposes(t))
    if already_hidden:
        # A no-op exclusion usually means the operator expected a different base,
        # or typed "-" for "_" inside a "+" name.
        verb, pronoun = ("is", "it") if len(already_hidden) == 1 else ("are", "them")
        raise ConfigError(
            f"Invalid {ENV} exclusion list: {', '.join(already_hidden)} {verb} already hidden "
            f"by {mode.base}; remove {pronoun} from the '{EXCLUDE_SEPARATOR}' list."
            f"{_HYPHEN_HINT if mode.allowlist else ''}"
        )

    result = ExposureResult()
    for tool in tools:
        if tool.name in excluded:
            result.excluded.append(tool.name)
        elif not base_exposes(tool):
            result.hidden.append(tool.name)
        else:
            continue
        mcp.local_provider.remove_tool(tool.name)
    result.hidden.sort()
    result.excluded.sort()
    return result
