# Initiator-first tracing

1. Select one exact network request evidence ID.
2. Call `get_request_initiator`.
3. Record stack frames, script URL/ID, and completeness.
4. Use `search_scripts` only when the initiator does not provide a sufficient handle.
5. Correlate search candidates with the request URL, stack, and captured timing.

A source string match alone does not establish that the matched code constructed or
dispatched the request.
