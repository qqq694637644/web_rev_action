# `list_console_errors`

## Contract

- **Operation:** `list_console_errors`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** return bounded console-error evidence associated with one experiment.
- **Consequential:** no.
- **Prerequisites:** an exact experiment ID.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.

Optional fields and defaults:

- `limit`: 1–500; default 100.

Decoded example:

```json
{"experiment_id":"exp_capture","limit":100}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "list_console_errors",
  "operation_contract_hash": "sha256:379c6f18f81509c5a8d0d3de32e1aede8eff3f3fca5fc9eabd70f3078b93b8dd",
  "payload_json": "{\"experiment_id\":\"exp_capture\",\"limit\":100}",
  "skill_content_hash": "sha256:f66c3d13f268880a753a0f46098997becee5f0b3ef299232d63f7fe0ef5f7d24",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: bounded console-message evidence, timestamps, severity/source metadata, artifact IDs when available, and count.

Safe retry: read-only. Increase the limit only when needed and preserve experiment association.

Typical errors: `invalid_operation_payload`, `experiment_not_found`.

Next recommended inspect operation: `get_experiment` or the evidence operation relevant to the error's associated request/script.

Contract hash: `sha256:379c6f18f81509c5a8d0d3de32e1aede8eff3f3fca5fc9eabd70f3078b93b8dd`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `ListConsoleErrorsRequest`
- Payload model: `ListConsoleErrorsPayload`
- Registry handler: `_inspect_list_console_errors`
- Consequential: `false`
- Operation contract hash: `sha256:379c6f18f81509c5a8d0d3de32e1aede8eff3f3fca5fc9eabd70f3078b93b8dd`

```json
{
  "additionalProperties": false,
  "properties": {
    "experiment_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Experiment Id",
      "type": "string"
    },
    "limit": {
      "default": 100,
      "maximum": 500,
      "minimum": 1,
      "title": "Limit",
      "type": "integer"
    }
  },
  "required": [
    "experiment_id"
  ],
  "title": "ListConsoleErrorsPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
