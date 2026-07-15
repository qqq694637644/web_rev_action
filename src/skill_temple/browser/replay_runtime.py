"""Load the standalone browser-context replay runtime."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_RUNTIME_PATH = Path(__file__).with_name("replay_runtime.js")

@lru_cache(maxsize=1)
def load_replay_runtime() -> str:
    """Return the reviewed JavaScript runtime asset."""
    return _RUNTIME_PATH.read_text(encoding="utf-8")
