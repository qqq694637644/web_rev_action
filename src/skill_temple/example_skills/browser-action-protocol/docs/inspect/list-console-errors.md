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
  "operation_contract_hash": "sha256:eb17de31b00a3bce940d691c4b29530ae193fc2d61bcc32273ab3dff336b7826",
  "payload_json": "{\"experiment_id\":\"exp_capture\",\"limit\":100}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: bounded console-message evidence, timestamps, severity/source metadata, artifact IDs when available, and count.

Safe retry: read-only. Increase the limit only when needed and preserve experiment association.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `invalid_adapter_response`. Missing or invalid live `messages` or `pagination` fields are protocol failures, not an empty console result. Pagination page/index/next-page facts must be mutually consistent.

Next recommended inspect operation: `get_experiment` or the evidence operation relevant to the error's associated request/script.

Contract hash: `sha256:eb17de31b00a3bce940d691c4b29530ae193fc2d61bcc32273ab3dff336b7826`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:eb17de31b00a3bce940d691c4b29530ae193fc2d61bcc32273ab3dff336b7826`
<!-- END GENERATED CONTRACT -->
