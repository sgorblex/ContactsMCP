"""JSON-lines logging to stderr and the audit line."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from contacts_mcp.logs import AUDIT_LOGGER, JsonFormatter, audit, audit_scope, configure_logging


def _record(msg: str, *args: object, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("contacts_mcp.test", logging.WARNING, __file__, 1, msg, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter() -> None:
    line = JsonFormatter().format(_record("hello %s", "world", account="work"))
    entry = json.loads(line)
    assert entry["level"] == "warning"
    assert entry["logger"] == "contacts_mcp.test"
    assert entry["msg"] == "hello world"
    assert entry["account"] == "work"
    assert entry["ts"].endswith("+00:00")
    assert "args" not in entry


def test_json_formatter_drops_uvicorn_color_message() -> None:
    entry = json.loads(JsonFormatter().format(_record("x", color_message="\x1b[1mx\x1b[0m")))
    assert "color_message" not in entry


def test_configure_logging_writes_json_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("INFO")
        logging.getLogger("uvicorn.access").info("GET /healthz")
        logging.getLogger("contacts_mcp.x").debug("hidden")
        captured = capsys.readouterr()
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    assert captured.out == ""
    [line] = captured.err.splitlines()
    assert json.loads(line)["msg"] == "GET /healthz"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    names = ("fastmcp", "mcp", AUDIT_LOGGER)
    saved = (root.handlers[:], root.level, [logging.getLogger(n).level for n in names])
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, level in zip(names, saved[2], strict=True):
        logging.getLogger(name).setLevel(level)


@pytest.mark.usefixtures("restore_logging")
def test_audit_lines_survive_a_higher_log_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    logging.getLogger("contacts_mcp.x").info("hidden")
    audit(tool="update_contact", account="work", resource_name="people/c1", outcome="ok")
    [line] = capsys.readouterr().err.splitlines()
    assert json.loads(line)["kind"] == "audit"


@pytest.mark.usefixtures("restore_logging")
def test_debug_level_keeps_third_party_debug_lines_out(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG")
    logging.getLogger("fastmcp.server.auth").debug("Authorization code not found: abc")
    logging.getLogger("mcp.server").debug("hidden too")
    logging.getLogger("contacts_mcp.x").debug("shown")
    [line] = capsys.readouterr().err.splitlines()
    assert json.loads(line)["msg"] == "shown"


def test_audit_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger=AUDIT_LOGGER)
    audit(
        tool="update_contact",
        account="work",
        resource_name="people/c1",
        fields=["phoneNumbers", "emailAddresses", "phoneNumbers"],
        outcome="ok",
    )
    [record] = caplog.records
    entry = json.loads(JsonFormatter().format(record))
    assert entry["kind"] == "audit"
    assert entry["user"] == "local"
    assert entry["tool"] == "update_contact"
    assert entry["account"] == "work"
    assert entry["resource_name"] == "people/c1"
    assert entry["fields"] == ["emailAddresses", "phoneNumbers"]
    assert entry["outcome"] == "ok"


def test_audit_scope_records_that_an_audit_line_was_written() -> None:
    with audit_scope() as scope:
        assert not scope.audited
        audit(tool="t", account=None, resource_name=None, outcome="refused")
        assert scope.audited
    with audit_scope() as fresh:
        assert not fresh.audited
    # Outside any scope, audit() still works.
    audit(tool="t", account=None, resource_name=None, outcome="error")
