"""Tool errors: short, actionable messages that reach the model.

All derive from fastmcp's ``ToolError``, whose message is passed to the client
as is. Never put a raw HTTP body, a token or a client secret in a message.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError


class ContactsError(ToolError):
    """Base class for every error a tool reports."""


class InvalidArgumentError(ContactsError):
    """A tool argument is malformed or inconsistent (checked before any API call)."""


class AccountError(ContactsError):
    """Unknown or ambiguous account, or an account that cannot be used right now."""


class ReadOnlyAccountError(AccountError):
    """A write tool was called on an account marked read_only."""


class NeedsRelinkError(AccountError):
    """The refresh token was revoked or expired (``invalid_grant``)."""

    def __init__(self, alias: str) -> None:
        super().__init__(
            f"Account '{alias}' must be linked again: Google rejected its refresh token "
            f"(revoked or expired). Run `contacts-mcp auth add {alias}` on the server."
        )
        self.alias = alias


class GoogleApiError(ContactsError):
    """A People API error, mapped to a short message."""

    def __init__(
        self, message: str, *, status: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class NotFoundError(GoogleApiError):
    """The contact, group or other contact does not exist (404)."""


class EtagConflictError(GoogleApiError):
    """The person changed since it was read (400 FAILED_PRECONDITION on the etag)."""


class PermissionDeniedError(GoogleApiError):
    """403: scope missing, API disabled, or the client not allowed by a Workspace admin."""


class RateLimitedError(GoogleApiError):
    """429 or 5xx still failing after the bounded retries."""
