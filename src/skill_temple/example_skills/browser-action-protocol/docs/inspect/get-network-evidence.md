# `get_network_evidence`

## Contract

- **Operation:** `get_network_evidence`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** return one exact redacted network-request evidence entry.
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
  "operation": "get_network_evidence",
  "operation_contract_hash": "sha256:45fe0f176119a664fd872bfe1fa61fe0e3b1814021a3a2cb8ded59acd452ba46",
  "payload_json": "{\"evidence_id\":\"ev_network_request\",\"experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:f66c3d13f268880a753a0f46098997becee5f0b3ef299232d63f7fe0ef5f7d24",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: exact request identities, selector ID, redacted request/response summary, completeness, artifact IDs, and relative paths. Credential artifact contents remain local.

Safe retry: read-only. If the ID is wrong, return to `list_evidence`; do not choose the first similar URL.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `evidence_not_found`, `evidence_kind_mismatch`.

Next recommended inspect operations: `get_request_shape` and `get_request_initiator` for the same exact evidence ID.

Contract hash: `sha256:45fe0f176119a664fd872bfe1fa61fe0e3b1814021a3a2cb8ded59acd452ba46`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `GetNetworkEvidenceRequest`
- Payload model: `GetNetworkEvidencePayload`
- Registry handler: `_inspect_get_network_evidence`
- Consequential: `false`
- Operation contract hash: `sha256:45fe0f176119a664fd872bfe1fa61fe0e3b1814021a3a2cb8ded59acd452ba46`

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
  "title": "GetNetworkEvidencePayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
