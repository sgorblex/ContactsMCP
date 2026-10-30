"""The account file store: atomic writes, modes, load errors, scan, and reload via the registry."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from contacts_mcp.accounts import (
    AccountFileError,
    AccountFileStore,
    AccountRecord,
    validate_alias,
)
from contacts_mcp.config import GoogleClientConfig
from contacts_mcp.fields import SCOPES
from contacts_mcp.google.people import PeopleClient
from contacts_mcp.registry import AccountRegistry

TOKEN = "1//refresh-token-very-secret"


def record(alias: str = "work", **overrides: Any) -> AccountRecord:
    fields: dict[str, Any] = {
        "alias": alias,
        "email": f"{alias}@example.com",
        "refresh_token": SecretStr(TOKEN),
        "scopes": list(SCOPES),
        "read_only": False,
        "linked_at": datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return AccountRecord(**fields)


@pytest.fixture
def real_store(tmp_path: Path) -> AccountFileStore:
    return AccountFileStore(tmp_path / "data")


def listing(store: AccountFileStore) -> list[str]:
    return sorted(p.name for p in store.accounts_dir.iterdir())


# --- aliases ----------------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["a", "work", "my-account-2", "0x", "a" * 32])
def test_valid_aliases(alias: str) -> None:
    assert validate_alias(alias) == alias


@pytest.mark.parametrize("alias", ["", "Work", "-a", "a_b", "a.b", "a/b", "../x", "a" * 33, "é"])
def test_invalid_aliases(alias: str) -> None:
    with pytest.raises(ValueError, match="invalid account alias"):
        validate_alias(alias)


# --- save / load ------------------------------------------------------------------------------


def test_round_trip(real_store: AccountFileStore) -> None:
    original = record(read_only=True, email=None)
    path = real_store.save(original)
    assert path == real_store.accounts_dir / "work.json"

    loaded = real_store.load("work")
    assert loaded == original
    assert loaded.refresh_token.get_secret_value() == TOKEN


def test_file_holds_the_token_as_plain_text_and_utc_timestamp(real_store: AccountFileStore) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    real_store.save(record(linked_at=datetime(2026, 3, 1, 17, 30, tzinfo=ist)))

    data = json.loads((real_store.accounts_dir / "work.json").read_text())
    assert data["refresh_token"] == TOKEN
    assert data["linked_at"] == "2026-03-01T12:00:00+00:00"
    assert data["alias"] == "work"
    assert data["scopes"] == list(SCOPES)
    assert data["version"] == 1


def test_naive_timestamps_are_read_as_utc(real_store: AccountFileStore) -> None:
    real_store.save(record())
    path = real_store.accounts_dir / "work.json"
    data = json.loads(path.read_text())
    data["linked_at"] = "2026-03-01T12:00:00"
    path.write_text(json.dumps(data))

    assert real_store.load("work").linked_at == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_modes(real_store: AccountFileStore) -> None:
    path = real_store.save(record())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(real_store.accounts_dir.stat().st_mode) == 0o700


def test_modes_survive_a_permissive_umask(real_store: AccountFileStore) -> None:
    old = os.umask(0)
    try:
        path = real_store.save(record())
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(real_store.accounts_dir.stat().st_mode) == 0o700


def test_overwrite_keeps_mode_and_replaces_content(real_store: AccountFileStore) -> None:
    real_store.save(record())
    path = real_store.save(record(email="new@example.com"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert real_store.load("work").email == "new@example.com"


def test_loose_existing_directory_is_tightened(real_store: AccountFileStore) -> None:
    real_store.accounts_dir.mkdir(parents=True)
    real_store.accounts_dir.chmod(0o755)
    real_store.save(record())
    assert stat.S_IMODE(real_store.accounts_dir.stat().st_mode) == 0o700


def test_no_temp_file_left_behind(real_store: AccountFileStore) -> None:
    real_store.save(record())
    real_store.save(record())
    assert listing(real_store) == ["work.json"]


def test_temp_file_is_created_private(
    real_store: AccountFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp file is born 0600 (never chmod-ed after the token is written)."""
    seen: list[tuple[str, int]] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any) -> None:
        seen.append((Path(src).name, stat.S_IMODE(Path(src).stat().st_mode)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    real_store.save(record())
    [(name, mode)] = seen
    assert name.startswith(".work.json.")
    assert name.endswith(".tmp")
    assert mode == 0o600


def test_failure_before_rename_keeps_the_old_file(
    real_store: AccountFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_store.save(record(email="old@example.com"))
    before = (real_store.accounts_dir / "work.json").read_bytes()

    def boom(src: Any, dst: Any) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk on fire"):
        real_store.save(record(email="new@example.com", refresh_token=SecretStr("other")))
    monkeypatch.undo()

    assert (real_store.accounts_dir / "work.json").read_bytes() == before
    assert listing(real_store) == ["work.json"]
    assert real_store.load("work").email == "old@example.com"


def test_failure_while_writing_leaves_nothing(
    real_store: AccountFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="fsync failed"):
        real_store.save(record())
    monkeypatch.undo()
    assert listing(real_store) == []


def test_save_rejects_an_invalid_alias(real_store: AccountFileStore) -> None:
    with pytest.raises(ValueError, match="invalid account alias"):
        real_store.save(record(alias="../evil"))
    assert not real_store.accounts_dir.exists()


# --- load errors ------------------------------------------------------------------------------


def test_load_missing(real_store: AccountFileStore) -> None:
    with pytest.raises(AccountFileError, match="no such account file"):
        real_store.load("work")


def test_load_invalid_alias(real_store: AccountFileStore) -> None:
    with pytest.raises(AccountFileError, match="invalid account alias"):
        real_store.load("Not Valid")


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"{not json", "invalid JSON"),
        (b"\xff\xfe\x00", "invalid JSON"),
        (b"[1, 2]", "not a JSON object"),
        (b"{}", "invalid content"),
    ],
)
def test_load_bad_content(real_store: AccountFileStore, content: bytes, reason: str) -> None:
    real_store.accounts_dir.mkdir(parents=True)
    (real_store.accounts_dir / "work.json").write_bytes(content)
    with pytest.raises(AccountFileError, match=reason) as info:
        real_store.load("work")
    assert "work.json" in str(info.value)


def test_load_wrong_alias_in_file(real_store: AccountFileStore) -> None:
    real_store.save(record("home"))
    (real_store.accounts_dir / "home.json").rename(real_store.accounts_dir / "work.json")
    with pytest.raises(AccountFileError, match="does not match its name"):
        real_store.load("work")


def test_errors_never_contain_the_token(real_store: AccountFileStore) -> None:
    real_store.accounts_dir.mkdir(parents=True)
    bad = {
        "alias": "work",
        "refresh_token": TOKEN,
        "scopes": TOKEN,  # wrong type: pydantic would echo the input value
        "linked_at": TOKEN,
    }
    (real_store.accounts_dir / "work.json").write_text(json.dumps(bad))
    with pytest.raises(AccountFileError) as info:
        real_store.load("work")
    assert TOKEN not in str(info.value)
    assert "scopes" in str(info.value)

    (real_store.accounts_dir / "work.json").write_text(f'{{"refresh_token": "{TOKEN}" oops')
    with pytest.raises(AccountFileError) as info2:
        real_store.load("work")
    assert TOKEN not in str(info2.value)


def test_record_repr_hides_the_token() -> None:
    assert TOKEN not in repr(record())


# --- delete -----------------------------------------------------------------------------------


def test_delete(real_store: AccountFileStore) -> None:
    real_store.save(record())
    real_store.delete("work")
    assert listing(real_store) == []
    with pytest.raises(AccountFileError, match="no such account file"):
        real_store.delete("work")


# --- scan -------------------------------------------------------------------------------------


def test_scan_missing_directory(real_store: AccountFileStore) -> None:
    assert real_store.scan() == {}


def test_scan_mixed_files(real_store: AccountFileStore) -> None:
    real_store.save(record("work"))
    real_store.save(record("home", read_only=True))
    d = real_store.accounts_dir
    (d / "broken.json").write_text("{nope")
    (d / "Bad_Name.json").write_text("{}")
    (d / "mismatch.json").write_text((d / "work.json").read_text())
    (d / "notes.txt").write_text("ignored")
    (d / ".work.json.deadbeef.tmp").write_text("half-written")
    (d / ".hidden.json").write_text("{}")
    (d / "dir.json").mkdir()

    found = real_store.scan()

    assert sorted(found) == ["Bad_Name", "broken", "dir", "home", "mismatch", "work"]
    assert isinstance(found["work"], AccountRecord)
    assert isinstance(found["home"], AccountRecord)
    assert found["home"].read_only is True
    for stem in ("broken", "Bad_Name", "mismatch", "dir"):
        assert isinstance(found[stem], AccountFileError)
    assert "not a valid account alias" in str(found["Bad_Name"])
    assert "invalid JSON" in str(found["broken"])
    assert "does not match" in str(found["mismatch"])


def test_scan_ignores_leftover_temp_files(real_store: AccountFileStore) -> None:
    real_store.save(record())
    (real_store.accounts_dir / ".work.json.0123456789abcdef.tmp").write_text("{")
    assert list(real_store.scan()) == ["work"]


# --- reload through the real registry ---------------------------------------------------------


def make_registry(
    store: AccountFileStore, google_client: GoogleClientConfig, http: httpx.AsyncClient
) -> AccountRegistry:
    return AccountRegistry(
        store, google_client, http, people_factory=lambda h, t: PeopleClient(h, t)
    )


async def test_registry_reload_picks_up_new_files_and_reports_bad_ones(
    real_store: AccountFileStore, google_client: GoogleClientConfig, http: httpx.AsyncClient
) -> None:
    registry = make_registry(real_store, google_client, http)
    registry.reload()
    assert registry.aliases() == []

    real_store.save(record("work"))
    registry.reload()
    assert registry.aliases() == ["work"]
    assert registry.default_alias() == "work"
    assert registry.resolve(None).email == "work@example.com"

    real_store.save(record("home", read_only=True))
    (real_store.accounts_dir / "broken.json").write_text("{nope")
    registry.reload()

    assert registry.aliases() == ["broken", "home", "work"]
    infos = {a.alias: a.info() for a in registry.all()}
    assert infos["work"].status == "ok"
    assert infos["home"].read_only is True
    assert infos["broken"].status == "unreadable"
    assert infos["broken"].detail is not None
    assert "invalid JSON" in infos["broken"].detail
    assert TOKEN not in infos["broken"].detail


async def test_registry_reload_after_relink_and_removal(
    real_store: AccountFileStore, google_client: GoogleClientConfig, http: httpx.AsyncClient
) -> None:
    real_store.save(record("work"))
    registry = make_registry(real_store, google_client, http)
    registry.reload()
    first = registry.resolve("work")

    # unchanged token: runtime state is kept, flags are refreshed
    real_store.save(record("work", read_only=True))
    registry.reload()
    assert registry.resolve("work") is first
    assert first.read_only is True

    # re-linked (new token): a fresh account
    real_store.save(record("work", refresh_token=SecretStr("another-token")))
    registry.reload()
    assert registry.resolve("work") is not first

    real_store.delete("work")
    registry.reload()
    assert registry.aliases() == []


async def test_registry_reload_of_a_file_fixed_in_place(
    real_store: AccountFileStore, google_client: GoogleClientConfig, http: httpx.AsyncClient
) -> None:
    real_store.accounts_dir.mkdir(parents=True)
    (real_store.accounts_dir / "work.json").write_text("garbage")
    registry = make_registry(real_store, google_client, http)
    registry.reload()
    assert registry.all()[0].status == "unreadable"

    real_store.save(record("work"))
    registry.reload()
    assert registry.all()[0].status == "ok"
