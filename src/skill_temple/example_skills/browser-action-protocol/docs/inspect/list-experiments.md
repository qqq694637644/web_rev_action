# `list_experiments`

## Contract

- **Operation:** `list_experiments`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** list bounded experiment summaries, optionally filtered by session.
- **Consequential:** no.
- **Prerequisites:** none; use a session filter when known.

## Decoded payload schema

Required fields: none.

Optional fields and defaults:

- `session_id`: safe identifier, max 128 characters.
- `limit`: 1–200; default 50.

Decoded example:

```json
{"session_id":"analysis-main","limit":20}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "list_experiments",
  "operation_contract_hash": "sha256:c25eb04e31f242889cf19123558bc7131f7ed1ca1056851ab4e380f547edfc05",
  "payload_json": "{\"limit\":20,\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: bounded experiment summaries, exact `experiment_id` values, status, session association, and count.

Safe retry: read-only; correct validation errors when `dispatch_started=false`.

Typical errors: `invalid_operation_payload`.

Next recommended inspect operation: `get_experiment` for one selected exact ID.

Contract hash: `sha256:c25eb04e31f242889cf19123558bc7131f7ed1ca1056851ab4e380f547edfc05`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:c25eb04e31f242889cf19123558bc7131f7ed1ca1056851ab4e380f547edfc05`
<!-- END GENERATED CONTRACT -->
