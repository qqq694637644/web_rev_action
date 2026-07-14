# Generic experiment design

Design the smallest experiment that answers one open question from the current-site inventory.

## Exact source selection

A replay source must include both:

```json
{
  "experiment_id": "exp_source",
  "evidence_id": "ev_network_source"
}
```

Do not infer a source request from endpoint order, URL similarity, or the first observation in a manifest.

## Bindings and extractors

Use bindings for values that must be resolved before dispatch:

| Source | Use |
|---|---|
| `generated` | fresh IDs, nonce, timestamp |
| `preserve_source` | exact value from source snapshot |
| `extractor` | value from a named prior-response extractor |
| `literal` | fixed experiment constant |
| `manual_input` | caller-provided experiment input |

Extractor failure is an observation unless `required=true`. Every exported extractor snapshot is a credential artifact and must be cited by artifact ID rather than copied into a report.

Bindings run first. Mutations run afterward in list order.

The service always adds the reserved `replay_request` network-evidence selector for
the exact outbound request. User selectors are supporting selectors and are appended;
do not use the reserved selector ID. Integer occurrence values are non-negative.
Add-header and add-query operations use only `append`.

Audit ordered operations with two separate facts: whether the operation was applied
to the replay spec, and whether its intermediate value remains independently visible
on the final wire. A value intentionally replaced or removed by a later operation is
`overwritten_by_later_operation`, not ineffective.

## Mutation discipline

The generic API supports multiple mutations, but one target mutation per experiment is usually easier to interpret. Record the actual outbound wire observation before drawing a conclusion.

Query mutation defaults to `preserve_raw`. Use `normalize` only when canonical re-encoding is part of the experiment.

## Comparison

Comparison is optional. Each reference requires exactly one exact selector:

```json
{
  "experiment_id": "exp_reference",
  "evidence_id": "ev_network_reference"
}
```

or:

```json
{
  "experiment_id": "exp_reference",
  "observation_id": "obs_reference"
}
```

Available dimensions are intentionally narrow:

```text
request_body
response_status
response_content_type
stream_summary
environment
```

`stream_summary` compares canonical raw-event count, semantic-event count, terminal reason, and primary event source. It does not imply stream payload equality.

## Reader and termination

Configure reader limits separately from terminal conditions:

```text
response_reader.max_bytes
response_reader.max_events
termination.conditions
```

Allowed conditions:

```text
exact_sse_data
text_pattern
network_close
idle_window
```

Rules:

- at most one `exact_sse_data` condition;
- at most one `idle_window` condition;
- `text_pattern.value` must be non-empty;
- `network_close` has no value or event name;
- `idle_window` uses `window_ms` and no value or event name.
- an empty condition list normalizes to `network_close`;
- runtime termination reason and matched condition must agree.

The normalized termination configuration and the effective capture/requirements are
saved in `replay.replay_protocol`. The original normalized request is saved in
`replay.requested_replay_protocol`; their hashes identify requested versus effective
configuration. When comparing the current stream, use only the unique observation
linked to `replay.network_evidence_id`. Do not fall back to the first observation.

`mode=auto` starts stream capture as a probe. Evidence requirements are selected after
dispatch from the runtime `observed_response_mode`: ordinary uses ordinary network
evidence; SSE, NDJSON, and raw stream require their stream evidence and terminal
contract. HTTP status does not select the mode. Explicit stream readers may accept a
missing or non-standard Content-Type when the runtime mode matches; auto mode must
remain consistent with the Content-Type-based automatic selection rule.

## Strong-evidence Stop template

When the page exposes a Stop control and the goal is to classify a user cancellation, the following is a useful strong-evidence template:

```text
send/start action
→ wait for first event or an event predicate
→ click Stop
→ wait for network canceled/finished/timeout observation
```

This is a recommended experiment design, not a backend validity rule. A flow without these checkpoints can still execute; cancellation attribution remains unknown or unclassified when evidence is insufficient.
