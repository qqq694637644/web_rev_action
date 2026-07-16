# `get_request_initiator`

## Contract

- **Operation:** `get_request_initiator`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** return bounded initiator evidence for one exact network request.
- **Consequential:** no.
- **Prerequisites:** exact `experiment_id` and network-request `evidence_id` from `list_evidence`.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.
- `evidence_id`: safe identifier, max 256 characters.

Optional fields: none.

Decoded example:

```json
{"experiment_id":"exp_capture","evidence_id":"ev_network_request"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "get_request_initiator",
  "operation_contract_hash": "sha256:ea6094207fd9c0dc02c6af6e1567be8378918597c8eb717e1462bd9fc6fcffc2",
  "payload_json": "{\"evidence_id\":\"ev_network_request\",\"experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:f66c3d13f268880a753a0f46098997becee5f0b3ef299232d63f7fe0ef5f7d24",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: initiator stack/artifact metadata, script URL or script ID candidates, exact request association, and completeness.

Safe retry: read-only. If initiator evidence is absent, design a narrower recapture rather than broad source guessing.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `evidence_not_found`, `evidence_kind_mismatch`, `request_initiator_missing`.

Next recommended inspect operation: `search_scripts` or `get_script_source` using the identified script handle.

Contract hash: `sha256:ea6094207fd9c0dc02c6af6e1567be8378918597c8eb717e1462bd9fc6fcffc2`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `GetRequestInitiatorRequest`
- Payload model: `GetRequestInitiatorPayload`
- Registry handler: `_inspect_get_request_initiator`
- Consequential: `false`
- Operation contract hash: `sha256:ea6094207fd9c0dc02c6af6e1567be8378918597c8eb717e1462bd9fc6fcffc2`

```json
{
  "additionalProperties": false,
  "properties": {
    "evidence_id": {
      "maxLength": 256,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Evidence Id",
      "type": "string"
    },
    "experiment_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Experiment Id",
      "type": "string"
    }
  },
  "required": [
    "experiment_id",
    "evidence_id"
  ],
  "title": "GetRequestInitiatorPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
