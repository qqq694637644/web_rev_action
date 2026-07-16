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
  "operation_contract_hash": "sha256:c36fae4a178b20cbe8576ce6269877fc0cb41be81645e4d031f9d086fb8a10ce",
  "payload_json": "{\"evidence_id\":\"ev_network_request\",\"experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: exact request identities, selector ID, redacted request/response summary, completeness, artifact IDs, and relative paths. Credential artifact contents remain local.

Safe retry: read-only. If the ID is wrong, return to `list_evidence`; do not choose the first similar URL.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `evidence_not_found`, `evidence_kind_mismatch`.

Next recommended inspect operations: `get_request_shape` and `get_request_initiator` for the same exact evidence ID.

Contract hash: `sha256:c36fae4a178b20cbe8576ce6269877fc0cb41be81645e4d031f9d086fb8a10ce`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:c36fae4a178b20cbe8576ce6269877fc0cb41be81645e4d031f9d086fb8a10ce`
<!-- END GENERATED CONTRACT -->
