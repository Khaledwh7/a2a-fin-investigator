"""Structured JSON logging with secret redaction.

One-line JSON logs are grep-able and ship straight into any log aggregator.
Crucially, we **redact** anything that looks like a credential before it is
written — that's data-leakage protection: a stray token in a log is a breach.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_SENSITIVE_KEYS = {"authorization", "token", "access_token", "api_key", "apikey",
                   "secret", "jwt_secret", "password", "anthropic_api_key"}
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I)


def redact(obj: Any) -> Any:
    """Recursively mask sensitive values; also scrub bearer tokens in strings."""
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in _SENSITIVE_KEYS else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _BEARER.sub(r"\1***", obj)
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # structured extras passed via logger.info(..., extra={"fields": {...}})
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(redact(fields))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json"
                         else logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Quiet chatty third-party request logs; our own events stay at INFO.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a structured event: message = event name, everything else in `fields`."""
    logger.info(event, extra={"fields": fields})


def log_warning(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Same as :func:`log_event`, at WARNING — for degraded-but-handled conditions."""
    logger.warning(event, extra={"fields": fields})
