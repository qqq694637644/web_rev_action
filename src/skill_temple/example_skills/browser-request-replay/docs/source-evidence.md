# Replay source evidence

A valid source must be exact `network_request` evidence with saved method, URL, raw
query representation, ordered headers, body bytes or structured body, and enough local
credential provenance for browser-context execution.

Inspect the source with `get_network_evidence`. Use `get_request_shape` before JSON
Pointer mutations. Reject sources that are partial, ambiguous, from another account
context, or missing the request snapshot required by the objective.
