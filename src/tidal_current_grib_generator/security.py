"""Credential-safe logging and diagnostics helpers."""

from __future__ import annotations

import re

REDACTED = "<redacted>"
SECRET_KEYS = ("password", "token", "secret", "credential", "api_key", "apikey", "username", "user", "email")
SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:x-cop-user|username|user|email|token|access_token|api_key|apikey|password)=)([^&#\\s]+)"
)


def redact_value(key: str, value: object) -> object:
    if any(secret in key.lower() for secret in SECRET_KEYS) and value not in (None, ""):
        return REDACTED
    return value


def redact_mapping(values: dict[str, object]) -> dict[str, object]:
    return {key: redact_value(key, redact_object(value)) for key, value in values.items()}


def redact_object(value: object) -> object:
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_object(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_object(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str, sensitive_values: list[str] | tuple[str, ...] = ()) -> str:
    redacted = str(text)
    redacted = SENSITIVE_QUERY_RE.sub(r"\1" + REDACTED, redacted)
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(str(value), REDACTED)
    return redacted
