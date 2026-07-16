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
  "operation_contract_hash": "sha256:d24fcb46ac1f983990cedbcf388b62a5a8011be54e8dbeb7bd1cfc09f555ef17",
  "payload_json": "{\"limit\":20,\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:786f2331d061583e44fc9dc7344bae933a380d13006b65d1e88f4ae31ad64e6e",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: bounded experiment summaries, exact `experiment_id` values, status, session association, and count.

Safe retry: read-only; correct validation errors when `dispatch_started=false`.

Typical errors: `invalid_operation_payload`.

Next recommended inspect operation: `get_experiment` for one selected exact ID.

Contract hash: `sha256:d24fcb46ac1f983990cedbcf388b62a5a8011be54e8dbeb7bd1cfc09f555ef17`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `ListExperimentsRequest`
- Payload model: `ListExperimentsPayload`
- Registry handler: `_inspect_list_experiments`
- Consequential: `false`
- Operation contract hash: `sha256:d24fcb46ac1f983990cedbcf388b62a5a8011be54e8dbeb7bd1cfc09f555ef17`

```json
{
  "additionalProperties": false,
  "properties": {
    "limit": {
      "default": 50,
      "maximum": 200,
      "minimum": 1,
      "title": "Limit",
      "type": "integer"
    },
    "session_id": {
      "anyOf": [
        {
          "maxLength": 128,
          "pattern": "^[a-zA-Z0-9_.-]+$",
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Session Id"
    }
  },
  "title": "ListExperimentsPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
