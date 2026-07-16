# `get_session`

## Contract

- **Operation:** `get_session`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** return bounded lifecycle and page metadata for one browser session.
- **Consequential:** no.
- **Prerequisites:** a known or proposed session ID.

## Decoded payload schema

Required fields:

- `session_id`: safe identifier matching `[A-Za-z0-9_.-]+`, max 128 characters.

Optional fields: none.

Decoded example:

```json
{"session_id":"analysis-main"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "get_session",
  "operation_contract_hash": "sha256:678486890fadfab343752c0a7feefd89339b0de41082648ddd59746259eb7031",
  "payload_json": "{\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:f66c3d13f268880a753a0f46098997becee5f0b3ef299232d63f7fe0ef5f7d24",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `session_id`, status, service instance ownership, selected page, timestamps, and stale reason when applicable.

Safe retry: read-only; validation errors with `dispatch_started=false` may be corrected. A not-found result means the caller may choose `open_session` rather than retrying inspection.

Typical errors: `invalid_operation_payload`, `session_not_found`.

Next recommended operation: `open_session` when absent/closed, otherwise the intended capture or inspection operation.

Contract hash: `sha256:678486890fadfab343752c0a7feefd89339b0de41082648ddd59746259eb7031`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `GetSessionRequest`
- Payload model: `GetSessionPayload`
- Registry handler: `_inspect_get_session`
- Consequential: `false`
- Operation contract hash: `sha256:678486890fadfab343752c0a7feefd89339b0de41082648ddd59746259eb7031`

```json
{
  "additionalProperties": false,
  "properties": {
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
  "title": "GetSessionPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
