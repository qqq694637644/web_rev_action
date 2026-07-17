# Busy and stale ownership

For `browser_busy` or `session_busy`, inspect the coordinator-owned session and active
experiment. Poll an active job rather than opening a parallel session or capture.

For a stale session, record the stale reason. A service-instance change means the old
Playwright reference cannot be trusted. Open a deliberate new/replacement session and
re-establish page alignment before capture.

Never clear ownership by deleting evidence or session files.
