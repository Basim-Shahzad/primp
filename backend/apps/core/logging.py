"""Structured JSON logging with request ID correlation."""

import json
import logging
from contextvars import ContextVar
from typing import Any

from .redact import redact

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class RedactingLogRecord(logging.LogRecord):
    """A LogRecord whose message never carries emails, phone numbers, tokens or OTP codes."""

    def getMessage(self) -> str:
        return redact(super().getMessage())


def install_redaction() -> None:
    """Make every log record redact its message (handlers, caplog and Sentry all see the result)."""
    logging.setLogRecordFactory(RedactingLogRecord)


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "request_id":
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
