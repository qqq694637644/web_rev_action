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
  "operation_contract_hash": "sha256:840368a8b4e8bfc424e6ff6ba7fdf099741a3a4ae727d23a65a30c8885b60699",
  "payload_json": "{\"evidence_id\":\"ev_network_request\",\"experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: initiator stack/artifact metadata, script URL or script ID candidates, exact request association, and completeness.

Safe retry: read-only. If initiator evidence is absent, design a narrower recapture rather than broad source guessing.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `evidence_not_found`, `evidence_kind_mismatch`, `request_initiator_missing`.

Next recommended inspect operation: `search_scripts` or `get_script_source` using the identified script handle.

Contract hash: `sha256:840368a8b4e8bfc424e6ff6ba7fdf099741a3a4ae727d23a65a30c8885b60699`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:840368a8b4e8bfc424e6ff6ba7fdf099741a3a4ae727d23a65a30c8885b60699`
<!-- END GENERATED CONTRACT -->
