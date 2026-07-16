# `cancel_experiment`

## Contract

- **Operation:** `cancel_experiment`
- **Action:** `runBrowserExperiment`
- **Purpose:** request cancellation of one known experiment owned by a browser session.
- **Consequential:** yes; it can interrupt active browser work and collector cleanup.
- **Prerequisites:** exact `experiment_id` and owning `session_id`; inspect the experiment first when practical.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.
- `session_id`: safe identifier, max 128 characters.

Optional fields: none.

Constraints: the experiment must belong to the supplied session. Cancellation does not imply rollback of remote side effects.

Decoded example:

```json
{"experiment_id":"exp_running","session_id":"analysis-main"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "cancel_experiment",
  "operation_contract_hash": "sha256:a7421f7619f8113fbfd5ee71ebf8bf3a9f9c07fa66756d71c801eb29c8b70a74",
  "payload_json": "{\"experiment_id\":\"exp_running\",\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:786f2331d061583e44fc9dc7344bae933a380d13006b65d1e88f4ae31ad64e6e",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `experiment_id`, `session_id`, terminal/current status, manifest path, and collector cleanup state.

Safe retry: after dispatch started, inspect `get_experiment`; do not issue repeated cancellation solely because the client lost the response.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `experiment_session_mismatch`, `browser_busy`, `operation_outcome_unknown`.

Next recommended inspect operation: `get_experiment`.

Contract hash: `sha256:a7421f7619f8113fbfd5ee71ebf8bf3a9f9c07fa66756d71c801eb29c8b70a74`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `CancelExperimentRequest`
- Payload model: `CancelExperimentPayload`
- Registry handler: `dispatch_cancel_experiment`
- Consequential: `true`
- Operation contract hash: `sha256:a7421f7619f8113fbfd5ee71ebf8bf3a9f9c07fa66756d71c801eb29c8b70a74`

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
    "session_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "experiment_id",
    "session_id"
  ],
  "title": "CancelExperimentPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
