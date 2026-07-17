# Session state machine

States are `absent`, `open`, `closed`, and `stale`.

1. Call `get_session` for the intended ID.
2. If absent, call `open_session` once.
3. If open, verify service ownership, page metadata, and alignment before reuse.
4. If stale or closed, open a new deliberate session ID or explicitly replace the old one.
5. While an experiment is running, do not open, close, or start another capture on the same shared browser.
6. Close only after terminal experiment inspection and artifact persistence.

Busy and unknown-outcome errors are state questions, not retry signals. Load
`browser-experiment-recovery` when the state cannot be established safely.
