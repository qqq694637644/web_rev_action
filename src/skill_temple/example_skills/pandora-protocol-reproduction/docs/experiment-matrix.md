# Experiment matrix

## Series fields

Use one `analysis_series_id` for the complete investigation. Set `predecessor_experiment_id` explicitly and increase `sequence_index` monotonically.

| Sequence | `scenario_type` | Required observation |
|---:|---|---|
| 0 | `baseline` | Current page, conversation list, config/session requests, console baseline |
| 1 | `first_message` | Conversation creation, first user/assistant IDs, parent ID, stream shape |
| 2 | `second_message` | Reuse of conversation ID, new parent/current node, mapping update |
| 3 | `regenerate` | Branch origin, replaced assistant node, current-node movement |
| 4 | `edit_message` | New user branch, descendant handling, mapping/current-node result |
| 5 | `stop_generation` | Event before Stop, Stop action, network/control/page-state outcome |
| 6 | `reload_verify` | Persisted conversation tree and selected branch after reload/retrieval |

## Network evidence selectors

Select only request classes needed for the current experiment. Typical selectors include:

```text
account_or_session
feature_or_model_config
conversation_create
conversation_detail
message_submit
conversation_list_or_title
stop_or_cancel_control
```

Use `export_parts=["all"]` for requests that may become replay sources. Use bounded `max_matches` and a narrow URL/method matcher.

## Field mutation matrix

Create one Control replay followed by one Treatment replay per row. Treatment submits only `control_experiment_id` and exactly one `mutation`; all other execution settings are inherited from the Control pair protocol.

Declare volatile bindings on the Control:

| Value class | Default policy | Reason |
|---|---|---|
| message/request ID, nonce, timestamp | `fresh_equivalent` | Avoid duplicate or expired values |
| conversation ID, fixed parent/context | explicit `same_value` when required | Preserve shared test context |

`fresh_equivalent` values differ physically but are normalized before non-target request comparison.

Use JSON Pointer rather than dotted JSONPath:

```text
/messages/0/id
/messages/0/content/parts/0
/parent_message_id
```

| Candidate | Mutation | Validation beyond HTTP status |
|---|---|---|
| conversation ID | remove/replace JSON Pointer | New or rejected conversation, persisted tree |
| parent message ID | remove/replace JSON Pointer | Branch attachment and current node |
| user message ID | remove/replace JSON Pointer | Server-generated vs client-required identity |
| model/variant | remove/replace JSON path | Model selection and stream metadata |
| timezone/locale | remove JSON path | Response semantics vs tracking only |
| client tracking ID | remove JSON/query/header | Request acceptance and persisted state |
| Authorization | remove header | Authentication failure without exposing value |
| custom CSRF header | remove header | Session/CSRF failure mode |
| query feature flag | remove/replace query parameter | Feature behavior and response schema |

Classify each result as `required`, `conditionally_required`, `optional`, `tracking_only`, or `unknown`.

Before classification require:

```text
pair_protocol_hash equal
Control target baseline present
target_delta_observed true
non_target_fields_equivalent true
volatile_bindings_effective true
mutation_effective true
pair_environment_comparison status = observed_equivalent
```

Only `remove + HTTP 400/422 + structured field_required at the exact target`
supports `required`. Replace rejection is `constrained_value`; HTTP 409 is
`conflict`. Authentication, rate-limit, server, generic 4xx, redirect, missing
Content-Type, incomplete response body, and weak text matches remain
inconclusive.

Do not test browser-managed Cookie, Origin, Referer, Host, Content-Length, or `Sec-*` through header mutation. Use a dedicated browser-context credential experiment when needed.

## Stop observation template

The Stop scenario should follow:

```text
submit message
→ wait first_event or request_observed
→ record primary request ID/event index
→ click Stop
→ observe one of network terminal, control request, event predicate,
  selector state, or bounded timeout window
→ verify page and conversation state
```

Do not require `network_canceled`. Record the observed classification.
