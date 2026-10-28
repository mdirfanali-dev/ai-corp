"""Remove secrets from anything written to logs.

Two layers: exact values read from ``config/secrets.env`` (strongest) and patterns for common
credential formats. Install it once per process with :func:`install`; the audit log then scrubs
every event before it is hashed and stored.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MARK = "[REDACTED]"
MIN_SECRET_LENGTH = 6

PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),                       # Telegram bot token
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b"),                          # Anthropic style keys
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),                         # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                   # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"(?i)\b(authorization:\s*bearer)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s'\",;]{6,}"),
]


class Redactor:
    def __init__(self, values: list[str] | None = None) -> None:
        cleaned = {v for v in (values or []) if len(v) >= MIN_SECRET_LENGTH}
        variants = set(cleaned) | {re.sub(r"\s", "", v) for v in cleaned}
        self._values = sorted((v for v in variants if len(v) >= MIN_SECRET_LENGTH), key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for value in self._values:
            if value in text:
                text = text.replace(value, MARK)
        for pattern in PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) + " " if m.lastindex else "") + MARK, text)
        return text

    def scrub_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v) for v in obj]
        return obj


def load_secret_values(home: Path) -> list[str]:
    path = home / "config" / "secrets.env"
    values: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                values.append(line.partition("=")[2].strip())
    except OSError:
        pass
    return [v for v in values if v]


def install(home: Path) -> Redactor:
    """Build a redactor from this data folder's secrets and make the audit log use it."""
    from . import events

    redactor = Redactor(load_secret_values(home))
    events.set_redactor(redactor.scrub)
    return redactor
