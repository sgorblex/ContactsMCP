"""The server shell: build_server, the write-audit middleware, SIGHUP, /healthz, stdio."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from contacts_mcp.accounts import AccountFileStore
from contacts_mcp.cli import main as cli_main
from contacts_mcp.errors import InvalidArgumentError
from contacts_mcp.google.oauth import TOKEN_URL
from contacts_mcp.logs import AUDIT_LOGGER, audit
from contacts_mcp.registry import AccountRegistry
from contacts_mcp.server import INSTRUCTIONS, HealthCheck, WriteAuditMiddleware, build_server
from tests._oidc import settings
from tests.conftest import FakeStore

READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False)


def test_build_server_stdio(make_registry: Callable[..., AccountRegistry]) -> None:
    mcp = build_server(settings(transport="stdio"), make_registry("work"))
    assert mcp.name == "contacts"
    assert mcp.instructions == INSTRUCTIONS
    assert mcp.auth is None
    assert any(getattr(r, "path", None) == "/healthz" for r in mcp._get_additional_http_routes())


async def test_unexpected_errors_reach_the_client_without_their_details(
    make_registry: Callable[..., AccountRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = make_registry("work")

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(registry.resolve("work").people, "get_person", broken)
    async with Client(build_server(settings(transport="stdio"), registry)) as client:
        with pytest.raises(ToolError) as err:
            await client.call_tool("get_contact", {"resource_name": "people/c1"})
    assert "internal detail" not in str(err.value)


@pytest.mark.parametrize("argv", [["serve", "extra"], ["sevre"]])
def test_cli_refuses_unknown_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit, match="usage: contacts-mcp"):
        cli_main(argv)


def test_instructions_say_what_matters() -> None:
    assert "`accounts` first" in INSTRUCTIONS
    assert "untrusted" in INSTRUCTIONS
    assert "Other contacts" in INSTRUCTIONS
    assert "lags behind writes" in INSTRUCTIONS


# -- write audit middleware ---------------------------------------------------------------


def audited_server() -> FastMCP:
    mcp = FastMCP("test")

    @mcp.tool(annotations=WRITE)
    async def create_contact(account: str, name: str) -> str:
        # Stands in for tools._common.write_attempt, which audits every attempt.
        audit(tool="create_contact", account=account, resource_name=None, outcome="ok")
        return name

    @mcp.tool(annotations=WRITE)
    async def update_contact(account: str, resource_name: str) -> str:
        audit(
            tool="update_contact", account=account, resource_name=resource_name, outcome="refused"
        )
        raise InvalidArgumentError("remove matched nothing")

    @mcp.tool(annotations=WRITE)
    async def precheck(account: str, resource_name: str) -> str:
        raise InvalidArgumentError(f"bad resource name {resource_name!r}")

    @mcp.tool(annotations=READ)
    async def get_contact(resource_name: str) -> str:
        return resource_name

    mcp.add_middleware(WriteAuditMiddleware({"create_contact", "update_contact", "precheck"}))
    return mcp


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if r.name == AUDIT_LOGGER]


async def test_unknown_argument_to_a_write_tool_is_audited_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    async with Client(audited_server()) as client:
        with pytest.raises(ToolError, match="Unexpected keyword argument"):
            await client.call_tool(
                "create_contact", {"account": "work", "name": "Ada", "nmae": "Ada Lovelace"}
            )
    [line] = audit_lines(caplog)
    assert line.kind == "audit"
    assert line.outcome == "refused"
    assert line.tool == "create_contact"
    assert line.account == "work"
    assert line.detail == "invalid arguments: nmae: unexpected_keyword_argument"
    # No argument values in the audit line.
    assert "Lovelace" not in json.dumps(vars(line), default=str)


async def test_wrong_type_is_audited(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    async with Client(audited_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("create_contact", {"account": "work", "name": {"x": 1}})
    [line] = audit_lines(caplog)
    assert line.outcome == "refused"
    assert line.detail == "invalid arguments: name: string_type"


async def test_tool_that_audits_is_not_audited_twice(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    async with Client(audited_server()) as client:
        await client.call_tool("create_contact", {"account": "work", "name": "Ada"})
        with pytest.raises(ToolError, match="matched nothing"):
            await client.call_tool(
                "update_contact", {"account": "work", "resource_name": "people/c1"}
            )
    assert [line.outcome for line in audit_lines(caplog)] == ["ok", "refused"]


async def test_refusal_before_write_attempt_is_audited(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    async with Client(audited_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("precheck", {"account": "home", "resource_name": "nope"})
    [line] = audit_lines(caplog)
    assert line.outcome == "refused"
    assert line.resource_name == "nope"
    # The message quotes the value; the audit line does not.
    assert line.detail == "invalid arguments"


async def test_read_tools_and_unknown_tools_are_not_audited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    async with Client(audited_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_contact", {"resource_name": "x", "extra": 1})
        with pytest.raises(ToolError):
            await client.call_tool("delete_contact", {"resource_name": "people/c1"})
    assert audit_lines(caplog) == []


async def test_build_server_audits_write_tools(
    make_registry: Callable[..., AccountRegistry],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from contacts_mcp import server

    def register(mcp: FastMCP, registry: AccountRegistry) -> None:
        @mcp.tool(annotations=WRITE)
        async def delete_contact(resource_name: str, account: str | None = None) -> str:
            return resource_name

    monkeypatch.setattr(server, "register_all", register)
    caplog.set_level("INFO")
    mcp = build_server(settings(transport="stdio"), make_registry("work"))
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("delete_contact", {"resource_name": "people/c1", "force": True})
    [line] = audit_lines(caplog)
    assert line.tool == "delete_contact"
    assert line.outcome == "refused"


# -- lifespan: SIGHUP and the shared HTTP client -------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
async def test_sighup_reloads_accounts(
    make_registry: Callable[..., AccountRegistry], store: FakeStore
) -> None:
    registry = make_registry("work")
    mcp = build_server(settings(transport="stdio"), registry)
    async with Client(mcp):
        store.add("home")
        os.kill(os.getpid(), signal.SIGHUP)
        for _ in range(100):
            if "home" in registry.aliases():
                break
            await asyncio.sleep(0.01)
        assert registry.aliases() == ["home", "work"]
    # The handler is gone with the server: SIGHUP is back to its previous disposition.
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_DFL


async def test_close_http_on_shutdown(store: FakeStore, google_client: Any) -> None:
    http = httpx.AsyncClient()
    registry = AccountRegistry(store, google_client, http)  # type: ignore[arg-type]
    mcp = build_server(settings(transport="stdio"), registry, close_http=True)
    async with Client(mcp):
        assert not http.is_closed
    assert http.is_closed


async def test_shared_http_client_is_left_open_by_default(
    make_registry: Callable[..., AccountRegistry],
) -> None:
    registry = make_registry()
    async with Client(build_server(settings(transport="stdio"), registry)):
        pass
    assert not registry.http.is_closed


# -- /healthz ---------------------------------------------------------------------------


async def healthz(registry: AccountRegistry, query: str = "") -> httpx.Response:
    from starlette.applications import Starlette
    from starlette.routing import Route

    check = HealthCheck(registry)
    app = Starlette(routes=[Route("/healthz", check.__call__)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        return await client.get("/healthz" + query)


async def test_healthz_without_accounts(make_registry: Callable[..., AccountRegistry]) -> None:
    response = await healthz(make_registry())
    assert response.status_code == 200
    assert response.text.startswith("ok: no accounts linked")


async def test_healthz_lists_unreadable_accounts(
    make_registry: Callable[..., AccountRegistry], store: FakeStore
) -> None:
    store.break_("old")
    store.break_("bad")
    response = await healthz(make_registry("work"))
    assert response.status_code == 503
    assert response.text == "2 unreadable account file(s)"
    assert "bad" not in response.text  # aliases stay out of the unauthenticated answer


async def test_healthz_makes_no_google_call(
    make_registry: Callable[..., AccountRegistry], google: respx.MockRouter
) -> None:
    response = await healthz(make_registry("work", "home"))
    assert response.status_code == 200
    assert response.text == "ok: 2 accounts"
    assert not google.calls


async def test_healthz_deep_refreshes_tokens(
    make_registry: Callable[..., AccountRegistry], google: respx.MockRouter
) -> None:
    def token(request: httpx.Request) -> httpx.Response:
        if b"refresh-home" in request.content:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    google.post(TOKEN_URL).mock(side_effect=token)
    registry = make_registry("work", "home")
    check = HealthCheck(registry)

    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "query_string": b"deep=1", "headers": []})
    response = await check(request)
    assert response.status_code == 503
    assert bytes(response.body).decode() == (
        "1 account(s) must be linked again (`contacts-mcp auth add <alias>`)"
    )
    assert len(google.calls) == 2

    # Within the interval the result is reused: no new calls to Google.
    response = await check(request)
    assert response.status_code == 503
    assert len(google.calls) == 2


async def test_healthz_deep_ok(
    make_registry: Callable[..., AccountRegistry], google: respx.MockRouter
) -> None:
    response = await healthz(make_registry("work"), "?deep=1")
    assert response.status_code == 200
    assert len(google.calls) == 1


# -- stdio --------------------------------------------------------------------------------


def _store_implemented(tmp_path: Path) -> bool:
    try:
        AccountFileStore(tmp_path / "probe").scan()
    except NotImplementedError:
        return False
    return True


async def test_stdio_smoke(tmp_path: Path) -> None:
    """``python -m contacts_mcp`` speaks MCP on stdout and nothing else."""
    if not _store_implemented(tmp_path):
        pytest.skip("AccountFileStore is not implemented in this tree yet")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "CONTACTS_MCP_DATA_DIR": str(tmp_path / "data"),
        "GOOGLE_CLIENT_ID": "dummy-client-id",
        "GOOGLE_CLIENT_SECRET": "dummy-client-secret",
        "FASTMCP_HOME": str(tmp_path / "fastmcp"),
        "FASTMCP_CHECK_FOR_UPDATES": "off",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "contacts_mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=1 << 20,  # tools/list is one JSON line larger than the 64 KiB default
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    async def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        await proc.stdin.drain()

    async def receive() -> dict[str, Any]:
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        assert line, "the server closed stdout"
        message: dict[str, Any] = json.loads(line)
        assert message["jsonrpc"] == "2.0"
        return message

    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "1"},
                },
            }
        )
        init = await receive()
        assert init["id"] == 1
        assert init["result"]["serverInfo"]["name"] == "contacts"
        assert "accounts" in init["result"]["instructions"]
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = await receive()
        assert listed["id"] == 2
        assert isinstance(listed["result"]["tools"], list)
    finally:
        proc.stdin.close()
        rest, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    # Whatever else reached stdout is protocol too; logs went to stderr as JSON lines.
    for line in rest.splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"
    for log_line in stderr.decode().splitlines():
        assert "level" in json.loads(log_line)


async def test_config_error_exits_2_without_traceback(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "CONTACTS_MCP_DATA_DIR": str(tmp_path / "data"),
        "CONTACTS_MCP_TRANSPORT": "http",
        "GOOGLE_CLIENT_ID": "dummy-client-id",
        "GOOGLE_CLIENT_SECRET": "dummy-client-secret",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "contacts_mcp",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode == 2
    assert stdout == b""
    assert b"CONTACTS_MCP_BASE_URL" in stderr
    assert b"Traceback" not in stderr
