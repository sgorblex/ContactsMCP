"""``contacts-mcp`` entry point: ``contacts-mcp`` serves, ``contacts-mcp auth …`` links accounts."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "auth":
        from contacts_mcp import auth_cli

        raise SystemExit(auth_cli.main(args[1:]))
    if args and (args[0] != "serve" or len(args) > 1):
        raise SystemExit(f"unexpected {args!r}; usage: contacts-mcp [serve | auth …]")
    from contacts_mcp import server

    server.main()
