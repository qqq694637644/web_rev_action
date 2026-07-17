"""Shared factual session lifecycle classifications."""

from __future__ import annotations

TERMINAL_CLOSED = frozenset({"closed", "closed_after_alignment_failure"})

NO_ATTACHMENT_STATES = frozenset(
    {
        "open_failed_before_dispatch",
        "open_canceled_before_dispatch",
    }
)

REUSABLE_SESSION_STATES = TERMINAL_CLOSED | NO_ATTACHMENT_STATES

MAY_HOLD_ATTACHMENT = frozenset(
    {
        "opening",
        "aligning",
        "open",
        "open_failed",
        "open_unaligned",
        "open_outcome_unknown",
        "alignment_failed",
        "close_failed",
        "close_outcome_unknown",
    }
)

STALE_ON_SERVICE_CHANGE = MAY_HOLD_ATTACHMENT
