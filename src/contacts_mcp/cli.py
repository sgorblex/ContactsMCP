"""``contacts-mcp`` entry point: ``contacts-mcp auth …`` links accounts."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "auth":
        from contacts_mcp import auth_cli

        raise SystemExit(auth_cli.main(args[1:]))
    raise SystemExit("usage: contacts-mcp auth …")
