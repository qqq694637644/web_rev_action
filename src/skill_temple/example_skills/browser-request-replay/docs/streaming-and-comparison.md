# Streaming and comparison

Choose response reading from observed evidence: ordinary, SSE, NDJSON, raw stream, or
auto when the transport is uncertain. Define termination separately from HTTP status.

Compare only named dimensions against exact reference experiment/evidence IDs. Record:

- HTTP status and content type;
- observed response mode;
- terminal condition and reason;
- stream event/byte completeness;
- environment differences;
- persistent state after replay.

A transport close is not automatically semantic completion. A successful response is
not automatically proof that a removed field is globally optional.
