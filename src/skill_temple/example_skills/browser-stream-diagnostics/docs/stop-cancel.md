# Stop versus cancel

- A page stop control is a user-interface action and may send a normal protocol request.
- `cancel_experiment` interrupts the local experiment task and collector lifecycle.
- Closing a session is not cancellation and may hide the final state.

Preferred order for an accidental or long-running experiment:

1. inspect experiment and stream status;
2. cancel the exact experiment if necessary;
3. poll to terminal state;
4. inspect cleanup and evidence;
5. close the session only when no further inspection is required.
