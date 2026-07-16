---
name: browser-session-capture
description: Use for opening or reusing browser sessions, selecting the correct page, running the smallest evidence capture, polling background experiments, and closing safely. Do not use for replay mutation design or deep evidence interpretation.
---

# Browser session and capture workflow

Load `browser-action-protocol` with this Skill. This Skill decides session lifecycle,
target selection, minimal capture scope, and job completion; the protocol Skill owns
all field-level contracts.

## Required reading order

1. Read `docs/session-state-machine.md` before deciding whether to open, reuse, or close.
2. Read `docs/target-selection.md` before page selection or navigation.
3. Read `docs/minimal-capture.md` before `capture_flow`.
4. Read `docs/job-polling.md` whenever execution mode is `job`.
5. From `browser-action-protocol`, read the exact contracts for `get_session`,
   `open_session`, `capture_flow`, `get_experiment`, and `close_session` only when used.

## Invariants

- Inspect a known session ID before opening it.
- Reuse only an open session owned by the current service instance and aligned to the intended page.
- Capture the minimum evidence needed for the current claim.
- A `running` experiment is incomplete; poll to a terminal status.
- Do not repeat a run operation after an unknown outcome.
- Close only when no dependent experiment remains active and later work will not reuse the session.

## Completion standard

Return stable `session_id` and `experiment_id` handles, terminal experiment status,
manifest path, and explicit evidence gaps. Do not claim capture success from the initial
job response alone.
