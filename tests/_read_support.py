"""Helpers shared by the read-tool tests (not a test module)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import respx
from fastmcp import Client

from contacts_mcp.google.oauth import TOKEN_URL

type Handler = Callable[[httpx.Request], httpx.Response]


def per_account_tokens(google: respx.MockRouter) -> None:
    """Make the token endpoint hand out ``token-<alias>`` for the posted ``refresh-<alias>``."""

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        alias = form["refresh_token"][0].removeprefix("refresh-")
        return httpx.Response(200, json={"access_token": f"token-{alias}", "expires_in": 3600})

    google.post(TOKEN_URL).mock(side_effect=handler)


def bearer(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


def by_account(responses: dict[str, httpx.Response | Handler]) -> Handler:
    """A route handler answering by the account whose token the request carries."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = responses[bearer(request)]
        return answer(request) if callable(answer) else answer

    return handler


async def call(client: Client[Any], tool: str, **arguments: Any) -> dict[str, Any]:
    """Call a tool, returning its structured content (errors raise ``ToolError``)."""
    result = await client.call_tool(tool, arguments)
    assert result.structured_content is not None
    return result.structured_content


def requests_to(google: respx.MockRouter, suffix: str) -> list[httpx.Request]:
    return [c.request for c in google.calls if c.request.url.path.endswith(suffix)]
