"""Tests for the text/json formatters and secret redaction."""

import json
import logging

import pytest

from autogatus import logging_setup
from autogatus.logging_setup import (
    JsonFormatter,
    TextFormatter,
    register_secret,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _clear_secrets():
    # Isolate the module-level secret set between tests.
    saved = set(logging_setup._SECRETS)
    logging_setup._SECRETS.clear()
    yield
    logging_setup._SECRETS.clear()
    logging_setup._SECRETS.update(saved)


def _record(msg, args=(), level=logging.INFO, name="autogatus", extra=None):
    record = logging.LogRecord(name, level, "path.py", 1, msg, args, None)
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


def test_text_formatter_shape():
    line = TextFormatter().format(_record("hello %s", ("world",)))
    assert " autogatus INFO hello world" in line
    # Starts with an ISO8601 UTC timestamp.
    assert line[:4].isdigit() and "T" in line.split(" ")[0]


def test_json_formatter_is_valid_json_with_fields():
    out = JsonFormatter().format(_record("wrote %d endpoints", (5,)))
    obj = json.loads(out)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "autogatus"
    assert obj["message"] == "wrote 5 endpoints"
    assert "timestamp" in obj


def test_json_formatter_includes_extras():
    out = JsonFormatter().format(_record(
        "access", extra={"http_method": "GET", "http_status": 200, "duration_ms": 1.5}))
    obj = json.loads(out)
    assert obj["http_method"] == "GET"
    assert obj["http_status"] == 200
    assert obj["duration_ms"] == 1.5


def test_redaction_text():
    register_secret("supersecrettoken123")
    line = TextFormatter().format(_record("using token %s now", ("supersecrettoken123",)))
    assert "supersecrettoken123" not in line
    assert "***REDACTED***" in line


def test_redaction_json_in_message_and_extra():
    register_secret("supersecrettoken123")
    out = JsonFormatter().format(_record(
        "token=%s", ("supersecrettoken123",),
        extra={"detail": "leaked supersecrettoken123 here"}))
    assert "supersecrettoken123" not in out
    assert "***REDACTED***" in out


def test_register_secret_ignores_short_or_empty():
    register_secret("")
    register_secret("abc")  # below the minimum length
    assert "" not in logging_setup._SECRETS
    assert "abc" not in logging_setup._SECRETS


def test_setup_logging_sets_levels():
    setup_logging(level="DEBUG", fmt="json")
    assert logging.getLogger("autogatus").level == logging.DEBUG
    assert logging.getLogger().level == logging.WARNING
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, JsonFormatter)
    # Reset to a sane default so other tests are unaffected.
    setup_logging(level="INFO", fmt="text")
