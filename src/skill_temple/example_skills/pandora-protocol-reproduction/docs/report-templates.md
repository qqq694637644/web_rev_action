# Report and schema templates

Create all derived outputs under the analysis workspace. Do not modify original experiment evidence.

## Required output tree

```text
reports/
  protocol-map.md
  stream-events.md
  state-machine.md
  pandora-comparison.md
schemas/
  request.schema.json
  stream-events.schema.json
  conversation-state.md
scripts/
  replay-http.py
notes/
  open-questions.md
```

## Citation syntax

Every core claim must end with a compact evidence block:

```text
Evidence:
- experiment_id: exp_...
- evidence_id: ev_...
- artifact_ids: [art_..., art_...]
- quality: confirmed | supported | partial | unknown | contradicted
```

Do not cite temporary `reqid` alone.

## `reports/protocol-map.md`

Use these sections:

```text
# Protocol map
## Authentication and session
## Configuration and model discovery
## Conversation creation
## Message submission
## Conversation retrieval
## Title/list/update/delete operations
## Stop or cancellation control
## Field necessity matrix
## Known variants and error responses
```

The field necessity matrix should contain:

| Field or header | Exact case-sensitive path/name | Classification | Baseline experiment/evidence | Mutation experiment/evidence | Setup flow | Target wire observation | Comparison facts | Response analysis | State verification | Notes |
|---|---|---|---|---|---|---|---|---|---|---|

Never include credential values. Header names are allowed.

## `reports/stream-events.md`

Document:

- transport and MIME type;
- raw framing and newline behavior;
- event name/data structure;
- message IDs and parent/current-node fields;
- finish markers and network terminal behavior;
- cancellation/Stop observations;
- raw versus semantic evidence quality.

Include an event table:

| Sequence | Source | Event type | Important paths | State effect | Evidence |
|---:|---|---|---|---|---|

## `reports/state-machine.md`

Represent the experiment series explicitly:

```text
baseline
→ first_message
→ second_message
→ regenerate
→ edit_message
→ stop_generation
→ reload_verify
```

For every transition record:

```text
predecessor_experiment_id
experiment_id
conversation_key
previous current node
new current node
created/replaced message IDs
branch behavior
page-state observation
network/stream observation
```

## `reports/pandora-comparison.md`

Separate:

```text
confirmed equivalence
confirmed difference
implementation-specific behavior
unknown or untested behavior
```

Do not infer compatibility from similar field names alone. Require evidence from request shape, replay, stream semantics, or persisted conversation state.

## `schemas/request.schema.json`

Generate JSON Schema from observed and replay-tested request bodies. Add custom annotations where useful:

```json
{
  "x-evidence": {
    "experiments": ["exp_..."],
    "evidence_ids": ["ev_..."],
    "classification": "required"
  }
}
```

A field should be in `required` only after an explicit zero-mutation baseline, an
effective one-target remove replay, exact source and comparison evidence IDs, an
exact HTTP 400/422 structured `field_required` response at the target, and
persistent-state verification. Record missing or ambiguous comparison facts
explicitly. Replace failures belong under `constrained_value`; 409 belongs under
`conflict`.

## `schemas/stream-events.schema.json`

Model each observed event variant with `oneOf` when shapes differ. Preserve unknown fields using `additionalProperties` unless repeated evidence proves a closed schema.

## `schemas/conversation-state.md`

Define:

- conversation identifier;
- message identifier;
- parent relationship;
- mapping/tree representation;
- current node;
- regenerate branch semantics;
- edit branch semantics;
- Stop partial-node semantics;
- reload persistence.

## `scripts/replay-http.py`

This script is a derived convenience tool, not the authentication source of truth.

Rules:

- credentials come from environment variables or explicit placeholders;
- never embed captured Cookie, Authorization, CSRF, or token values;
- preserve a comment with source experiment/evidence IDs;
- preserve baseline/mutation experiment and exact evidence IDs when the script is derived from replay observations;
- expose required/optional fields as typed parameters;
- print bounded status and response diagnostics;
- support stream decoding only when the stream report confirms the framing.

## `notes/open-questions.md`

Track unresolved items with:

| Question | Why unresolved | Required next experiment | Current evidence | Risk if guessed |
|---|---|---|---|---|

Do not silently omit incomplete branches, partial captures, conflicting replay results, or missing credential provenance.
