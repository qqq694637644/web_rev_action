# Error recovery

Transport and validation failures return a structured error with `code`,
`operation`, `message`, `dispatch_started`, and `suggested_next_action`. Validation
errors may include JSON Pointer `issues`. Errors created after a session or experiment
handle exists also return the available `session_id`, `experiment_id`, and
`manifest_relative_path` so inspection does not depend on guessing local directories.

Recovery rules:

- `invalid_json`: correct JSON syntax; dispatch did not start.
- `payload_must_be_object`: encode one object; dispatch did not start.
- `unknown_operation`: read `docs/operation-index.md` and use the correct Action.
- `invalid_operation_payload`: read the exact operation contract and correct only
  the reported paths.
- `stale_operation_contract`: reload this exact Skill, copy its returned content hash,
  reread the exact operation doc, and copy the generated contract hash. Dispatch did
  not start.
- `browser_busy` or `session_busy`: inspect state and wait for the current owner.
- `session_id_in_use`: inspect the returned session ID or choose a new ID. Dispatch did
  not start and the existing record was not overwritten.
- `invalid_adapter_response`: inspect the returned session/experiment handles. The
  adapter call completed or was sent, but its operation-specific result shape was not
  trustworthy; do not reinterpret it as an empty result.
- `operation_outcome_unknown`, `dispatch_started=true`, or `outcome=unknown`: do not
  repeat the consequential call. Inspect the returned session and experiment handles.

Cancellation is factual: `canceled` means no adapter dispatch was confirmed;
`canceled_outcome_unknown` is used only when the adapter recorded that the consequential
command or MCP call had been sent.

Only retry automatically when `dispatch_started=false` and the correction is purely
syntactic or contractual.
