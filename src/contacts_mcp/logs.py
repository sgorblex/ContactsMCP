"""JSON-lines logging to stderr, and the audit log of write attempts.

stdout belongs to the MCP protocol in stdio mode: nothing here writes to it.
Never log tokens, client secrets, authorization codes or contact field values.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from contacts_mcp.identity import current_user

AUDIT_LOGGER = "contacts_mcp.audit"

#: Attributes every LogRecord has; anything else came in through ``extra=``.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's copy of msg with ANSI colors
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, plus any ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Route every logger (ours, fastmcp's, uvicorn's) to stderr as JSON lines."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastmcp", "mcp"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # DEBUG is for our own code: fastmcp's debug lines include OAuth client codes.
    third_party = max(logging.INFO, root.level)
    for name in ("fastmcp", "mcp"):
        logging.getLogger(name).setLevel(third_party)
    # Audit lines are written whatever the level: every write attempt leaves one.
    logging.getLogger(AUDIT_LOGGER).setLevel(logging.INFO)
    # httpx logs full request URLs at INFO; they carry no tokens, but are noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


AuditOutcome = Literal["ok", "refused", "error"]


@dataclass
class AuditScope:
    """One tool call, as seen by the audit middleware: did the tool write an audit line?"""

    audited: bool = False


_audit_scope: ContextVar[AuditScope | None] = ContextVar("contacts_mcp_audit_scope", default=None)


@contextmanager
def audit_scope() -> Iterator[AuditScope]:
    """Track whether :func:`audit` runs during a tool call (used to avoid double lines)."""
    scope = AuditScope()
    token = _audit_scope.set(scope)
    try:
        yield scope
    finally:
        _audit_scope.reset(token)


def audit(
    *,
    tool: str,
    account: str | None,
    resource_name: str | None,
    fields: Iterable[str] = (),
    outcome: AuditOutcome,
    detail: str | None = None,
    contacts: Iterable[str] = (),
) -> None:
    """One ``"kind":"audit"`` line per write attempt, refused ones included.

    ``fields`` are field *names* (``emailAddresses``), never values. ``detail``
    is a short reason for a refusal or an error, never an API body. ``contacts`` are
    the resource names of the contacts a batch or group-membership write touches.
    """
    scope = _audit_scope.get()
    if scope is not None:
        scope.audited = True
    logging.getLogger(AUDIT_LOGGER).info(
        "write %s",
        outcome,
        extra={
            "kind": "audit",
            "user": current_user(),
            "tool": tool,
            "account": account,
            "resource_name": resource_name,
            "contacts": list(contacts),
            "fields": sorted(set(fields)),
            "outcome": outcome,
            "detail": detail,
        },
    )
