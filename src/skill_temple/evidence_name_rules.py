"""Shared name classification rules for protocol evidence."""

from __future__ import annotations

_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}
_SENSITIVE_HEADER_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "api-key",
    "apikey",
    "session",
    "csrf",
    "xsrf",
)


def is_sensitive_header(name: str) -> bool:
    """Return whether a header name belongs to the existing sensitive-name set."""

    normalized = name.lower()
    return normalized in _SENSITIVE_HEADER_NAMES or any(
        fragment in normalized for fragment in _SENSITIVE_HEADER_FRAGMENTS
    )
