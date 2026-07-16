---
name: browser-request-replay
description: Use for exact browser-context request replay, source-evidence selection, one-variable mutation, extractor and binding order, streaming response handling, and factual comparisons. Do not use without a complete captured request snapshot.
---

# Browser request replay workflow

Load `browser-action-protocol` with this Skill. Replay only exact local network
evidence; never reconstruct credentials or request bodies from chat.

## Required reading order

1. Read `docs/source-evidence.md` before selecting a replay source.
2. Read `docs/binding-and-mutation-order.md` before changing any value.
3. Read `docs/streaming-and-comparison.md` for SSE, NDJSON, or raw responses.
4. Read `docs/credential-safety.md` before dispatch.
5. Read exact protocol contracts for `get_network_evidence`, `get_request_shape`,
   `replay_request`, `get_experiment`, and `list_evidence` when used.

## Invariants

- Source is one exact `experiment_id + evidence_id` with a complete request snapshot.
- Run a zero-mutation baseline before an intervention when comparison matters.
- Change one independent variable per experiment.
- Resolve extractors, then bindings, then ordered mutations.
- Keep browser-managed credentials local and untouched.
- Poll job-mode replay to terminal state before comparison.
- Treat server acceptance, semantic completion, and persistent state as separate outcomes.

## Completion standard

Return source and replay evidence IDs, mutation assessment, response/termination facts,
comparison references, and persistent-state evidence when relevant. Do not generalize
beyond the tested environment and variables.
