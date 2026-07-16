# `close_session`

## Contract

- **Operation:** `close_session`
- **Action:** `runBrowserExperiment`
- **Purpose:** detach and close one owned Playwright session.
- **Consequential:** yes; it changes session lifecycle state and can prevent later browser operations.
- **Prerequisites:** an existing session ID; cancel a mistaken active experiment separately rather than using close as cancellation.

## Decoded payload schema

Required fields:

- `session_id`: safe identifier, max 128 characters.

Optional fields and defaults:

- `deadline_ms`: 1000–42000; default 10000.

Constraints: close only the intended session. A persisted session from another service instance may already be stale.

Decoded example:

```json
{"session_id":"analysis-main","deadline_ms":10000}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "close_session",
  "operation_contract_hash": "sha256:2a89746555a4222c4d3c8c703645de9add82c3582f2bf95b75cf71cd86795983",
  "payload_json": "{\"deadline_ms\":10000,\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `session_id`, closed/stale status, and close reason.

Safe retry: when dispatch started or the outcome is unknown, call `get_session` first. Do not repeatedly close without inspecting current state.

Typical errors: `invalid_operation_payload`, `session_not_found`, `browser_busy`, `operation_outcome_unknown`.

Next recommended inspect operation: `get_session`.

Contract hash: `sha256:2a89746555a4222c4d3c8c703645de9add82c3582f2bf95b75cf71cd86795983`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `run`
- Consequential: `true`
- Operation contract hash: `sha256:2a89746555a4222c4d3c8c703645de9add82c3582f2bf95b75cf71cd86795983`
<!-- END GENERATED CONTRACT -->
