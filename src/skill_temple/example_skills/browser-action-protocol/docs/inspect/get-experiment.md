# `get_experiment`

## Contract

- **Operation:** `get_experiment`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** return bounded status and summary for one exact experiment.
- **Consequential:** no.
- **Prerequisites:** an exact `experiment_id` from a run response or `list_experiments`.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.

Optional fields: none.

Decoded example:

```json
{"experiment_id":"exp_capture"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "get_experiment",
  "operation_contract_hash": "sha256:90218fa0eeadf8306cbdaad27449244d9d25a31ae8f6ae192eefe0f7e35a6e51",
  "payload_json": "{\"experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:786f2331d061583e44fc9dc7344bae933a380d13006b65d1e88f4ae31ad64e6e",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: terminal/running status, session ID, quality summary, comparison results, evidence counts, and manifest relative path.

Safe retry: read-only. For job-mode runs, poll this operation until `completed`, `partial`, `failed`, or `interrupted`; do not repeat the original run while status is `running`.

Typical errors: `invalid_operation_payload`, `experiment_not_found`.

Next recommended inspect operation: `list_evidence` after a terminal state.

Contract hash: `sha256:90218fa0eeadf8306cbdaad27449244d9d25a31ae8f6ae192eefe0f7e35a6e51`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `GetExperimentRequest`
- Payload model: `GetExperimentPayload`
- Registry handler: `_inspect_get_experiment`
- Consequential: `false`
- Operation contract hash: `sha256:90218fa0eeadf8306cbdaad27449244d9d25a31ae8f6ae192eefe0f7e35a6e51`

```json
{
  "additionalProperties": false,
  "properties": {
    "experiment_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Experiment Id",
      "type": "string"
    }
  },
  "required": [
    "experiment_id"
  ],
  "title": "GetExperimentPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
