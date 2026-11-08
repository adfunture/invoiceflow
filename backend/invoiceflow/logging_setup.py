"""Strukturierte JSON-Logs mit Maskierung von Zugangsdaten."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_SENSITIVE_KEYS = re.compile(r"pass(word)?|secret|token|api[_-]?key|authorization|cookie|credential", re.I)
# Maskiert z. B. "password=abc" oder "postgresql://user:pw@host" in freiem Text.
_INLINE_PATTERNS = [
    (re.compile(r"(?i)(pass(?:word)?|secret|token|api[_-]?key)(\s*[=:]\s*)([^\s,;]+)"), r"\1\2***"),
    (re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)"), r"\1***\3"),
]

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def redact_text(text: str) -> str:
    for pattern, repl in _INLINE_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_value(key: str, value: object) -> object:
    if _SENSITIVE_KEYS.search(key):
        return "***"
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, str):
        return redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact_value(key, value)
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn-Accesslogs enthalten keine Secrets, aber Query-Strings; wir loggen Requests selbst.
    logging.getLogger("uvicorn.access").disabled = True
