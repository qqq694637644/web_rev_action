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
  "operation_contract_hash": "sha256:050522a6e549f9ad84ae9959e5c6b1f8955fd46ef3e5b10a25e48d7633e5bd29",
  "payload_json": "{\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `session_id`, factual lifecycle status, service instance ownership, selected page data when known, timestamps, adapter error metadata when present, and stale reason when applicable. Provisional and unknown open/close states remain inspectable.

Safe retry: read-only; validation errors with `dispatch_started=false` may be corrected. A not-found result means the caller may choose `open_session` rather than retrying inspection.

Typical errors: `invalid_operation_payload`, `session_not_found`.

Next recommended operation: `open_session` when absent/closed, otherwise the intended capture or inspection operation.

Contract hash: `sha256:050522a6e549f9ad84ae9959e5c6b1f8955fd46ef3e5b10a25e48d7633e5bd29`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:050522a6e549f9ad84ae9959e5c6b1f8955fd46ef3e5b10a25e48d7633e5bd29`
<!-- END GENERATED CONTRACT -->
