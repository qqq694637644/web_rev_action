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
  "operation_contract_hash": "sha256:562bef93cea0a9ad9502a97e1b31b6e240000834f9e4adeeaaad9a8992c4b073",
  "payload_json": "{\"experiment_id\":\"exp_capture\",\"kind\":\"network_request\",\"limit\":100}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: stable evidence IDs, evidence kinds, artifact IDs/paths, bounded summaries, and count.

Safe retry: read-only. Select by exact ID and kind; never infer identity from list position.

Typical errors: `invalid_operation_payload`, `experiment_not_found`.

Next recommended inspect operation: the exact evidence reader such as `get_network_evidence`, `get_request_shape`, or `get_request_initiator`.

Contract hash: `sha256:562bef93cea0a9ad9502a97e1b31b6e240000834f9e4adeeaaad9a8992c4b073`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:562bef93cea0a9ad9502a97e1b31b6e240000834f9e4adeeaaad9a8992c4b073`
<!-- END GENERATED CONTRACT -->
