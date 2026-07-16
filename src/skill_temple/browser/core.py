"""Stable browser service errors, deadlines, and identifiers."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

from .adapters.contracts import AdapterError


class BrowserServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        dispatch_started: bool = False,
        outcome: str | None = None,
        session_id: str | None = None,
        experiment_id: str | None = None,
        manifest_relative_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.dispatch_started = dispatch_started
        self.outcome = outcome
        self.session_id = session_id
        self.experiment_id = experiment_id
        self.manifest_relative_path = manifest_relative_path

    def with_context(
        self,
        *,
        session_id: str | None = None,
        experiment_id: str | None = None,
        manifest_relative_path: str | None = None,
    ) -> BrowserServiceError:
        if session_id is not None:
            self.session_id = session_id
        if experiment_id is not None:
            self.experiment_id = experiment_id
        if manifest_relative_path is not None:
            self.manifest_relative_path = manifest_relative_path
        return self


def service_error_from_adapter(
    exc: AdapterError,
    operation: str,
    *,
    consequential: bool,
) -> BrowserServiceError:
    dispatch_started = bool(exc.dispatch_started)
    outcome_unknown = bool(exc.outcome_unknown) if consequential else False
    if outcome_unknown:
        return BrowserServiceError(
            "operation_outcome_unknown",
            f"{operation} was dispatched but no trustworthy terminal result was received: {exc}",
            502,
            dispatch_started=True,
            outcome="unknown",
        )
    return BrowserServiceError(
        "browser_adapter_failed",
        f"{operation} failed at the browser adapter boundary: {exc}",
        502,
        dispatch_started=dispatch_started,
        outcome="failed",
    )


class Deadline:
    def __init__(self, timeout_ms: int) -> None:
        self.started_monotonic = time.monotonic()
        self.started_wall_time_ms = int(time.time() * 1000)
        self.deadline_monotonic = self.started_monotonic + timeout_ms / 1000
        self.deadline_wall_time_ms = self.started_wall_time_ms + timeout_ms
        self.timeout_ms = timeout_ms

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def remaining_ms(self) -> int:
        return max(0, int(self.remaining_seconds() * 1000))

    def ensure_remaining(self, operation: str) -> None:
        if self.remaining_seconds() <= 0:
            raise BrowserServiceError(
                "deadline_exceeded",
                f"Deadline exceeded before {operation}",
                504,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_ms": self.timeout_ms,
            "started_wall_time_ms": self.started_wall_time_ms,
            "deadline_wall_time_ms": self.deadline_wall_time_ms,
            "remaining_ms": self.remaining_ms(),
        }

    def child(self, timeout_ms: int) -> Deadline:
        child = object.__new__(Deadline)
        child.started_monotonic = time.monotonic()
        child.started_wall_time_ms = int(time.time() * 1000)
        requested_seconds = max(0.001, timeout_ms / 1000)
        child.deadline_monotonic = min(
            self.deadline_monotonic,
            child.started_monotonic + requested_seconds,
        )
        child.deadline_wall_time_ms = min(
            self.deadline_wall_time_ms,
            child.started_wall_time_ms + timeout_ms,
        )
        child.timeout_ms = min(timeout_ms, self.remaining_ms())
        return child


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", value):
        raise BrowserServiceError("invalid_identifier", f"Invalid {label}: {value}")
    return value
