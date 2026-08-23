"""Centralized logging setup for autogatus.

One shared "autogatus" logger, two output formats:

  text  human readable, one line per record (default)
  json  one JSON object per line, for log aggregators

Both honor AUTOGATUS_LOG_LEVEL. A redaction pass scrubs registered secret
values (the push token) from every emitted line, in either format, so a token
can never leak into logs even if it slips into a message or an extra field.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

# Values registered here are replaced with a placeholder in every log line.
_SECRETS: set[str] = set()
_REDACTED = "***REDACTED***"

# Minimum length to redact, so we never scrub short or empty strings that would
# turn ordinary output into a wall of asterisks.
_MIN_SECRET_LEN = 6

# LogRecord attributes that are not caller-supplied "extra" fields.
_STD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


def register_secret(value: str) -> None:
    """Register a value to be redacted from all future log output."""
    if value and len(value) >= _MIN_SECRET_LEN:
        _SECRETS.add(value)


def _redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    return text


def _iso(created: float) -> str:
    return datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="milliseconds")


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.asctime = _iso(record.created)
        line = f"{record.asctime} {record.name} {record.levelname} {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return _redact(line)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": _iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return _redact(json.dumps(payload, default=str))


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Configure the root handler and the autogatus logger.

    Third-party libraries (docker, urllib3, waitress) stay at WARNING so they do
    not drown out autogatus output, while the autogatus logger uses the chosen
    level. Everything flows through one handler with the selected formatter.
    """
    formatter = JsonFormatter() if fmt.lower() == "json" else TextFormatter()

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()  # stderr
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger("autogatus").setLevel(resolved)
