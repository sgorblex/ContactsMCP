"""CONTACTS_MCP_EXPOSED_TOOLS: parsing, pruning, and what clients can list and call."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from contacts_mcp.config import ConfigError
from contacts_mcp.exposure import (
    ExposureMode,
    apply_exposure,
    parse_exposed_tools,
    registered_tools,
)
from contacts_mcp.registry import AccountRegistry
from contacts_mcp.server import build_server
from tests._oidc import settings

READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def dummy_server() -> FastMCP:
    mcp = FastMCP("test")

    @mcp.tool(annotations=READ)
    async def get_contact(resource_name: str) -> str:
        return resource_name

    @mcp.tool(annotations=READ)
    async def list_other_contacts() -> str:
        return "others"

    @mcp.tool(annotations=WRITE)
    async def create_contact(name: str) -> str:
        return name

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_contact(resource_name: str) -> str:
        return resource_name

    @mcp.tool
    async def unannotated() -> str:
        return "?"

    return mcp


async def names(mcp: FastMCP) -> set[str]:
    async with Client(mcp) as client:
        return {t.name for t in await client.list_tools()}


# -- parsing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("all", ExposureMode("all")),
        (" ALL ", ExposureMode("all")),
        ("read-only", ExposureMode("read-only")),
        ("read-only+create_contact", ExposureMode("read-only", ("create_contact",))),
        (
            "read-only+create_contact, update_contact",
            ExposureMode("read-only", ("create_contact", "update_contact")),
        ),
        (
            "all-delete_contact,delete_group",
            ExposureMode("all", (), ("delete_contact", "delete_group")),
        ),
        (
            "read-only+create_contact-list_other_contacts",
            ExposureMode("read-only", ("create_contact",), ("list_other_contacts",)),
        ),
        ("read-only-list_other_contacts", ExposureMode("read-only", (), ("list_other_contacts",))),
    ],
)
def test_parse(value: str, expected: ExposureMode) -> None:
    assert parse_exposed_tools(value) == expected


def test_str_round_trips() -> None:
    value = "read-only+create_contact-list_other_contacts"
    assert str(parse_exposed_tools(value)) == value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("everything", "Expected one of: all, read-only"),
        ("readonly", "Expected one of"),
        ("all+create_contact", "only valid with read-only"),
        ("read-only+", "must name at least one tool"),
        ("all-", "must name at least one tool"),
        ("read-only+a-b+c", "Use one '+' list followed by one '-' list"),
        ("read-only+a-a", "both exposed with '+' and excluded with '-': a"),
    ],
)
def test_parse_errors_name_the_variable(value: str, message: str) -> None:
    with pytest.raises(ConfigError) as e:
        parse_exposed_tools(value)
    assert "CONTACTS_MCP_EXPOSED_TOOLS" in str(e.value)
    assert message in str(e.value)


# -- pruning ----------------------------------------------------------------------------


async def test_all_keeps_everything() -> None:
    mcp = dummy_server()
    result = apply_exposure(mcp, "all")
    assert result.hidden == []
    assert result.excluded == []
    assert len(await names(mcp)) == 5


async def test_read_only_hides_write_tools_from_list_and_call() -> None:
    mcp = dummy_server()
    result = apply_exposure(mcp, "read-only")
    assert result.hidden == ["create_contact", "delete_contact", "unannotated"]
    assert result.excluded == []
    assert await names(mcp) == {"get_contact", "list_other_contacts"}
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("create_contact", {"name": "x"})
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("delete_contact", {"resource_name": "people/c1"})
        result_ok = await client.call_tool("get_contact", {"resource_name": "people/c1"})
        assert result_ok.data == "people/c1"


async def test_read_only_plus_allowlist() -> None:
    mcp = dummy_server()
    result = apply_exposure(mcp, "read-only+create_contact")
    assert result.hidden == ["delete_contact", "unannotated"]
    assert await names(mcp) == {"get_contact", "list_other_contacts", "create_contact"}


async def test_exclusions_from_all() -> None:
    mcp = dummy_server()
    result = apply_exposure(mcp, "all-delete_contact,list_other_contacts")
    assert result.excluded == ["delete_contact", "list_other_contacts"]
    assert result.hidden == []
    assert await names(mcp) == {"get_contact", "create_contact", "unannotated"}


async def test_allowlist_and_exclusions_together() -> None:
    mcp = dummy_server()
    result = apply_exposure(mcp, "read-only+create_contact-list_other_contacts")
    assert result.excluded == ["list_other_contacts"]
    assert await names(mcp) == {"get_contact", "create_contact"}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("read-only+create_contct", "allowlist: unknown tool(s) create_contct"),
        ("all-delete_contct", "exclusion list: unknown tool(s) delete_contct"),
        # "create-contact" splits into "+create" and "-contact": point at the hyphen.
        ("read-only+create-contact", "Tool names use underscores"),
        ("read-only-delete_contact", "delete_contact is already hidden by read-only"),
    ],
)
def test_unknown_or_useless_names_stop_startup(value: str, message: str) -> None:
    with pytest.raises(ConfigError, match="CONTACTS_MCP_EXPOSED_TOOLS") as e:
        apply_exposure(dummy_server(), value)
    assert message in str(e.value)


async def test_apply_from_inside_an_event_loop() -> None:
    """build_server is synchronous but may be called from async code (tests, embedders)."""
    mcp = dummy_server()
    assert apply_exposure(mcp, "read-only").hidden


def test_build_server_rejects_a_bad_value(make_registry: Callable[..., AccountRegistry]) -> None:
    with pytest.raises(ConfigError, match="CONTACTS_MCP_EXPOSED_TOOLS"):
        build_server(settings(transport="stdio", exposed_tools="read-write"), make_registry())


def test_build_server_logs_hidden_tools(
    make_registry: Callable[..., AccountRegistry],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contacts_mcp import server

    def register_dummies(mcp: FastMCP, registry: AccountRegistry) -> None:
        for tool in registered_tools(dummy_server()):
            mcp.add_tool(tool)

    monkeypatch.setattr(server, "register_all", register_dummies)
    caplog.set_level("INFO")
    mcp = build_server(settings(transport="stdio", exposed_tools="read-only"), make_registry())
    assert mcp is not None
    hidden = [r for r in caplog.records if "hides" in r.getMessage()]
    assert hidden
    assert hidden[0].hidden_tools == ["create_contact", "delete_contact", "unannotated"]  # type: ignore[attr-defined]
    assert "create_contact" in hidden[0].getMessage()
