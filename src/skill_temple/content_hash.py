"""Cross-platform hashes for UTF-8 Skill text."""

from __future__ import annotations

import hashlib
from pathlib import Path


def canonical_text(text: str) -> str:
    """Normalize text newlines so checkout policy cannot change contract hashes."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_content_hash(text: str) -> str:
    """Return a SHA-256 over canonical UTF-8 text."""

    digest = hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def file_content_hash(path: Path) -> str:
    """Hash a strict UTF-8 text file independently of platform line endings."""

    return text_content_hash(path.read_text(encoding="utf-8", errors="strict"))
