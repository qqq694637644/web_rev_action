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
  "operation_contract_hash": "sha256:bc79ed315e19856450b20815474760de328baa3562bdd092495c6d5ace9f6593",
  "payload_json": "{\"deadline_ms\":10000,\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:786f2331d061583e44fc9dc7344bae933a380d13006b65d1e88f4ae31ad64e6e",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `session_id`, closed/stale status, and close reason.

Safe retry: when dispatch started or the outcome is unknown, call `get_session` first. Do not repeatedly close without inspecting current state.

Typical errors: `invalid_operation_payload`, `session_not_found`, `browser_busy`, `operation_outcome_unknown`.

Next recommended inspect operation: `get_session`.

Contract hash: `sha256:bc79ed315e19856450b20815474760de328baa3562bdd092495c6d5ace9f6593`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `CloseSessionRequest`
- Payload model: `CloseSessionPayload`
- Registry handler: `dispatch_close_session`
- Consequential: `true`
- Operation contract hash: `sha256:bc79ed315e19856450b20815474760de328baa3562bdd092495c6d5ace9f6593`

```json
{
  "additionalProperties": false,
  "properties": {
    "deadline_ms": {
      "default": 10000,
      "maximum": 42000,
      "minimum": 1000,
      "title": "Deadline Ms",
      "type": "integer"
    },
    "session_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "session_id"
  ],
  "title": "CloseSessionPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
