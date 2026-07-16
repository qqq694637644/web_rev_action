# `save_script_source`

## Contract

- **Operation:** `save_script_source`
- **Action:** `runBrowserExperiment`
- **Purpose:** persist one bounded script-source selection as auditable evidence linked to an experiment.
- **Consequential:** yes; it writes evidence artifacts and updates a manifest.
- **Prerequisites:** an open aligned session, an existing target experiment in that session, and a script URL or script ID identified by inspection.

## Decoded payload schema

Required fields:

- `session_id`.
- `target_experiment_id`.
- exactly one of `url` or `script_id`.

Optional fields and defaults:

- line range: `start_line` and `end_line` together.
- offset range: `offset` and `length` together; `length` max 200000.
- `initiator_evidence_id` and `evidence_label`.

Constraints: line and offset ranges are mutually exclusive; the target experiment must belong to the supplied session; an initiator ID must reference network-request evidence.

Decoded example:

```json
{"session_id":"analysis-main","script_id":"script-17","offset":0,"length":4000,"target_experiment_id":"exp_capture"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "save_script_source",
  "operation_contract_hash": "sha256:e644aa04a4da9d3e8cc35e0aa0180499b7adb594e1a71a951d934fe3214166d6",
  "payload_json": "{\"length\":4000,\"offset\":0,\"script_id\":\"script-17\",\"session_id\":\"analysis-main\",\"target_experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:786f2331d061583e44fc9dc7344bae933a380d13006b65d1e88f4ae31ad64e6e",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: script-source `evidence_id`, target `experiment_id`, SHA-256, artifact IDs, and relative artifact paths.

Safe retry: validation failures are safe to correct before dispatch. After dispatch started, inspect `list_evidence` on the target experiment before attempting another save to avoid duplicate evidence.

Typical errors: `invalid_operation_payload`, `script_target_session_mismatch`, `initiator_evidence_kind_invalid`, `session_not_found`, `operation_outcome_unknown`.

Next recommended inspect operation: `list_evidence` filtered to `script_source`.

Contract hash: `sha256:e644aa04a4da9d3e8cc35e0aa0180499b7adb594e1a71a951d934fe3214166d6`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `SaveScriptSourceRequest`
- Payload model: `SaveScriptSourcePayload`
- Registry handler: `dispatch_save_script_source`
- Consequential: `true`
- Operation contract hash: `sha256:e644aa04a4da9d3e8cc35e0aa0180499b7adb594e1a71a951d934fe3214166d6`

```json
{
  "additionalProperties": false,
  "properties": {
    "end_line": {
      "anyOf": [
        {
          "minimum": 1,
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "End Line"
    },
    "evidence_label": {
      "anyOf": [
        {
          "maxLength": 128,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Evidence Label"
    },
    "initiator_evidence_id": {
      "anyOf": [
        {
          "maxLength": 256,
          "pattern": "^[a-zA-Z0-9_.-]+$",
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Initiator Evidence Id"
    },
    "length": {
      "anyOf": [
        {
          "maximum": 200000,
          "minimum": 1,
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Length"
    },
    "offset": {
      "anyOf": [
        {
          "minimum": 0,
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Offset"
    },
    "script_id": {
      "anyOf": [
        {
          "maxLength": 512,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Script Id"
    },
    "session_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Session Id",
      "type": "string"
    },
    "start_line": {
      "anyOf": [
        {
          "minimum": 1,
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Start Line"
    },
    "target_experiment_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Target Experiment Id",
      "type": "string"
    },
    "url": {
      "anyOf": [
        {
          "maxLength": 8192,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Url"
    }
  },
  "required": [
    "session_id",
    "target_experiment_id"
  ],
  "title": "SaveScriptSourcePayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
