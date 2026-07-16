# `list_evidence`

## Contract

- **Operation:** `list_evidence`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** list bounded semantic evidence entries for one exact experiment.
- **Consequential:** no.
- **Prerequisites:** an exact experiment ID, preferably terminal.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.

Optional fields and defaults:

- `kind`: one exact evidence-kind filter, max 128 characters.
- `limit`: 1–500; default 100.

Decoded example:

```json
{"experiment_id":"exp_capture","kind":"network_request","limit":100}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "list_evidence",
  "operation_contract_hash": "sha256:f98fc845165362d1e76f45faaa9c403e4607db2c5df930c87fdc017a4ca740f9",
  "payload_json": "{\"experiment_id\":\"exp_capture\",\"kind\":\"network_request\",\"limit\":100}",
  "skill_content_hash": "sha256:f66c3d13f268880a753a0f46098997becee5f0b3ef299232d63f7fe0ef5f7d24",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: stable evidence IDs, evidence kinds, artifact IDs/paths, bounded summaries, and count.

Safe retry: read-only. Select by exact ID and kind; never infer identity from list position.

Typical errors: `invalid_operation_payload`, `experiment_not_found`.

Next recommended inspect operation: the exact evidence reader such as `get_network_evidence`, `get_request_shape`, or `get_request_initiator`.

Contract hash: `sha256:f98fc845165362d1e76f45faaa9c403e4607db2c5df930c87fdc017a4ca740f9`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `ListEvidenceRequest`
- Payload model: `ListEvidencePayload`
- Registry handler: `_inspect_list_evidence`
- Consequential: `false`
- Operation contract hash: `sha256:f98fc845165362d1e76f45faaa9c403e4607db2c5df930c87fdc017a4ca740f9`

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
    "kind": {
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
      "title": "Kind"
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
  "title": "ListEvidencePayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
