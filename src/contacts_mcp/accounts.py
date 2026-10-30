"""The account file store: one JSON file per alias under ``<data_dir>/accounts``.

Each file (mode 0600, directory 0700) holds the refresh token, the account's
e-mail, the scopes granted and the ``read_only`` flag. Files are written
atomically (temp file in the same directory, fsync, rename), so they back up
safely while the server runs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

log = logging.getLogger(__name__)

ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
FILE_VERSION = 1
FILE_MODE = 0o600
DIR_MODE = 0o700


class AccountFileError(Exception):
    """An account file is missing, unreadable or invalid; the message says which and why."""


def validate_alias(alias: str) -> str:
    """Return ``alias`` if it matches ``[a-z0-9][a-z0-9-]{0,31}``, else raise ValueError."""
    if not ALIAS_RE.fullmatch(alias):
        raise ValueError(
            f"invalid account alias {alias!r}: use 1-32 lowercase letters, digits and '-', "
            "starting with a letter or digit"
        )
    return alias


class AccountRecord(BaseModel):
    """The content of ``accounts/<alias>.json``."""

    model_config = ConfigDict(extra="ignore")

    version: int = FILE_VERSION
    alias: str
    email: str | None = None
    refresh_token: SecretStr
    scopes: list[str] = Field(default_factory=list)
    read_only: bool = False
    linked_at: datetime
    #: Which Google client linked the account, and so refreshes its token: the Desktop app
    #: one (the CLI) or the Web application one (the settings page).
    client: Literal["desktop", "web"] = "desktop"


class AccountFileStore:
    """Reads and writes account files. Synchronous: the files are tiny."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.accounts_dir = data_dir / "accounts"

    def path(self, alias: str) -> Path:
        return self.accounts_dir / f"{validate_alias(alias)}.json"

    def save(self, record: AccountRecord) -> Path:
        """Write atomically with mode 0600, creating the directory (0700) if needed.

        The content goes to a temp file created 0600 in the same directory, is flushed
        and fsynced, then renamed over the target; the directory is fsynced last. A
        failure at any point leaves the previous file untouched and no temp file behind.
        """
        target = self.path(record.alias)
        self._ensure_dir()
        linked_at = record.linked_at
        if linked_at.tzinfo is None:
            linked_at = linked_at.replace(tzinfo=UTC)
        payload: dict[str, Any] = {
            "version": record.version,
            "alias": record.alias,
            "email": record.email,
            "refresh_token": record.refresh_token.get_secret_value(),
            "scopes": list(record.scopes),
            "read_only": record.read_only,
            "linked_at": linked_at.astimezone(UTC).isoformat(),
            "client": record.client,
        }
        data = (json.dumps(payload, indent=2) + "\n").encode()

        tmp = self.accounts_dir / f".{record.alias}.json.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        self._fsync_dir()
        return target

    def load(self, alias: str) -> AccountRecord:
        """Raise AccountFileError if missing, unreadable, invalid, or its alias mismatches."""
        try:
            path = self.path(alias)
        except ValueError as e:
            raise AccountFileError(str(e)) from None
        return self._load_path(path, alias)

    def delete(self, alias: str) -> None:
        """Remove the file; AccountFileError if it does not exist."""
        try:
            path = self.path(alias)
        except ValueError as e:
            raise AccountFileError(str(e)) from None
        try:
            path.unlink()
        except FileNotFoundError:
            raise AccountFileError(f"{_label(alias)}: no such account file") from None
        except OSError as e:
            raise AccountFileError(f"{_label(alias)}: cannot delete ({e.strerror})") from e
        self._fsync_dir()

    def scan(self) -> dict[str, AccountRecord | AccountFileError]:
        """Every ``*.json`` in the accounts dir, by alias (file stem), loaded or failed.

        Files whose stem is not a valid alias are reported as AccountFileError under the
        stem. A missing accounts dir yields ``{}``. Temp files are ignored.
        """
        try:
            entries = sorted(self.accounts_dir.iterdir())
        except FileNotFoundError:
            return {}
        found: dict[str, AccountRecord | AccountFileError] = {}
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not name.endswith(".json"):
                continue
            stem = name[: -len(".json")]
            if not ALIAS_RE.fullmatch(stem):
                found[stem] = AccountFileError(
                    f"accounts/{name}: the file name is not a valid account alias "
                    "(1-32 lowercase letters, digits and '-')"
                )
                continue
            found[stem] = self._scan_one(entry, stem)
        return found

    # --- internals ---------------------------------------------------------------------

    def _scan_one(self, path: Path, alias: str) -> AccountRecord | AccountFileError:
        try:
            return self._load_path(path, alias)
        except AccountFileError as e:
            return e

    def _load_path(self, path: Path, alias: str) -> AccountRecord:
        label = _label(alias)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise AccountFileError(f"{label}: no such account file") from None
        except IsADirectoryError:
            raise AccountFileError(f"{label}: not a regular file") from None
        except OSError as e:
            raise AccountFileError(f"{label}: cannot read ({e.strerror})") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AccountFileError(f"{label}: invalid JSON ({e.msg})") from None
        except UnicodeDecodeError:
            raise AccountFileError(f"{label}: invalid JSON (not UTF-8 text)") from None
        if not isinstance(data, dict):
            raise AccountFileError(f"{label}: not a JSON object")
        try:
            record = AccountRecord.model_validate(data)
        except ValidationError as e:
            # Field names and messages only: the default report echoes input values.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'record'}: {err['msg']}"
                for err in e.errors(include_input=False, include_url=False)
            )
            raise AccountFileError(f"{label}: invalid content ({problems})") from None
        if record.alias != alias:
            raise AccountFileError(
                f"{label}: the file says alias {record.alias!r}, which does not match its name"
            )
        if record.linked_at.tzinfo is None:
            record = record.model_copy(update={"linked_at": record.linked_at.replace(tzinfo=UTC)})
        return record

    def _ensure_dir(self) -> None:
        self.accounts_dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        try:
            mode = self.accounts_dir.stat().st_mode & 0o777
            if mode & 0o077:
                self.accounts_dir.chmod(DIR_MODE)
                log.warning(
                    "tightened permissions of the accounts directory",
                    extra={"path": str(self.accounts_dir), "was": oct(mode)},
                )
        except OSError as e:
            log.warning(
                "accounts directory is accessible by others and cannot be tightened: %s",
                e.strerror,
            )

    def _fsync_dir(self) -> None:
        try:
            fd = os.open(self.accounts_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


def _label(alias: str) -> str:
    return f"accounts/{alias}.json"
