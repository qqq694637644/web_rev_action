# `get_request_shape`

## Contract

- **Operation:** `get_request_shape`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** read a bounded, paginated JSON request-shape view and optional redacted subtree for one exact network evidence item.
- **Consequential:** no.
- **Prerequisites:** exact `experiment_id` and network-request `evidence_id` with a saved request-shape artifact.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.
- `evidence_id`: safe identifier, max 256 characters.

Optional fields and defaults:

- `path_prefix`: RFC 6901 JSON Pointer; default `/`; max 512 characters.
- `page_idx`: 0–100000; default 0.
- `page_size`: 1–500; default 100.
- `max_depth`: 0–32; default 6.
- `max_array_items`: 1–200; default 20.
- `include_redacted_body`: default false.

Constraints: a non-root `path_prefix` must be a valid JSON Pointer. Keep subtree requests narrow.

Decoded example:

```json
{"experiment_id":"exp_capture","evidence_id":"ev_network_request","path_prefix":"/messages","page_size":50}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "get_request_shape",
  "operation_contract_hash": "sha256:e6ce42c0b0d3be8ce4910547a11b7a137153bb17b4ff38c99bbfc0cdeddaa2c2",
  "payload_json": "{\"evidence_id\":\"ev_network_request\",\"experiment_id\":\"exp_capture\",\"page_size\":50,\"path_prefix\":\"/messages\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: shape paths/descriptors, pagination, requested prefix, optional bounded redacted subtree, and exact evidence association.

Safe retry: read-only. Adjust pagination or bounds when truncated. If shape evidence is missing, capture a new exact request rather than guessing mutation paths.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `evidence_not_found`, `evidence_kind_mismatch`, `request_shape_missing`, `request_shape_invalid`.

Next recommended operation: `replay_request` only after an exact RFC 6901 target is selected.

Contract hash: `sha256:e6ce42c0b0d3be8ce4910547a11b7a137153bb17b4ff38c99bbfc0cdeddaa2c2`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:e6ce42c0b0d3be8ce4910547a11b7a137153bb17b4ff38c99bbfc0cdeddaa2c2`
<!-- END GENERATED CONTRACT -->
