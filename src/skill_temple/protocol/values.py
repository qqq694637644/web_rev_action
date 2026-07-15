"""JSON pointer and redacted value primitives shared by protocol modules."""

from __future__ import annotations

import re
from typing import Any


def _redact_json_value(value: Any, *, key_hint: str | None) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_json_value(child, key_hint=str(key)) for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(child, key_hint=key_hint) for child in value]
    return _redact_scalar(value, key_hint=key_hint)

def _redact_scalar(value: Any, *, key_hint: str | None) -> Any:
    raw_hint = key_hint or ""
    normalized = raw_hint.lower()
    if any(
        fragment in normalized
        for fragment in (
            "token",
            "secret",
            "password",
            "api-key",
            "apikey",
            "cookie",
            "authorization",
            "csrf",
            "xsrf",
            "session",
            "signature",
        )
    ):
        return "<redacted>"
    identifier_key = bool(
        raw_hint == "id" or normalized.endswith("_id") or re.search(r"(?:Id|ID)$", raw_hint)
    )
    if identifier_key:
        return "<identifier>"
    if not isinstance(value, str):
        return value
    if normalized in {"content", "parts", "text", "prompt", "query", "title"}:
        return "<text>"
    return "<string>"

def _decode_pointer(path: str) -> list[str]:
    if not path.startswith("/") or path == "/":
        raise ValueError(f"Invalid JSON Pointer: {path}")
    return [token.replace("~1", "/").replace("~0", "~") for token in path.split("/")[1:]]

def _encode_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")

def _last_pointer_token(path: str) -> str | None:
    try:
        return _decode_pointer(path)[-1]
    except (ValueError, IndexError):
        return None
