"""Credential-safe logging and diagnostics helpers."""

from __future__ import annotations

REDACTED = "<redacted>"
SECRET_KEYS = ("password", "token", "secret", "credential", "api_key", "apikey")


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
    return value
