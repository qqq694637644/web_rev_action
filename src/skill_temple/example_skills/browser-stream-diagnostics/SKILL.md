---
name: browser-stream-diagnostics
description: Use for SSE, NDJSON, and raw-stream diagnosis; distinguish transport close from semantic completion; inspect partial/interrupted/failed experiments; design termination conditions; and choose stop versus cancel safely.
---

# Browser stream diagnostics workflow

Load `browser-action-protocol` with this Skill. Use stream evidence and experiment
state together; HTTP status or socket close alone is not semantic completion.

## Required reading order

1. Read `docs/stream-modes.md` to classify the observed response.
2. Read `docs/completion-states.md` before interpreting terminal status.
3. Read `docs/termination-contract.md` before replay or long-running capture.
4. Read `docs/stop-cancel.md` before interruption.
5. Read exact protocol contracts for `get_stream_status`, `get_experiment`,
   `list_evidence`, `replay_request`, and `cancel_experiment` when used.

## Invariants

- Preserve raw bytes before relying on semantic parsing.
- Separate transport completion, parser completion, application completion, and persistent state.
- Treat `partial`, `interrupted`, and `failed` as distinct outcomes.
- A stop control observed in the page is not the same as backend cancellation.
- Define replay termination from observed protocol markers, not assumptions.
- After unknown outcome, inspect stream and experiment state before any retry.

## Completion standard

Report response mode, raw/parsed completeness, terminal reason, experiment status,
collector cleanup, evidence handles, and remaining ambiguity.
