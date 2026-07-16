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
  "operation_contract_hash": "sha256:c45c7c6374acbf91a7c66ce3baa9e10f609900a8f8c51212158541edaeb60ce5",
  "payload_json": "{\"evidence_id\":\"ev_network_request\",\"experiment_id\":\"exp_capture\",\"page_size\":50,\"path_prefix\":\"/messages\"}",
  "skill_content_hash": "sha256:786f2331d061583e44fc9dc7344bae933a380d13006b65d1e88f4ae31ad64e6e",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: shape paths/descriptors, pagination, requested prefix, optional bounded redacted subtree, and exact evidence association.

Safe retry: read-only. Adjust pagination or bounds when truncated. If shape evidence is missing, capture a new exact request rather than guessing mutation paths.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `evidence_not_found`, `evidence_kind_mismatch`, `request_shape_missing`, `request_shape_invalid`.

Next recommended operation: `replay_request` only after an exact RFC 6901 target is selected.

Contract hash: `sha256:c45c7c6374acbf91a7c66ce3baa9e10f609900a8f8c51212158541edaeb60ce5`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `GetRequestShapeRequest`
- Payload model: `GetRequestShapePayload`
- Registry handler: `_inspect_get_request_shape`
- Consequential: `false`
- Operation contract hash: `sha256:c45c7c6374acbf91a7c66ce3baa9e10f609900a8f8c51212158541edaeb60ce5`

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
    },
    "include_redacted_body": {
      "default": false,
      "title": "Include Redacted Body",
      "type": "boolean"
    },
    "max_array_items": {
      "default": 20,
      "maximum": 200,
      "minimum": 1,
      "title": "Max Array Items",
      "type": "integer"
    },
    "max_depth": {
      "default": 6,
      "maximum": 32,
      "minimum": 0,
      "title": "Max Depth",
      "type": "integer"
    },
    "page_idx": {
      "default": 0,
      "maximum": 100000,
      "minimum": 0,
      "title": "Page Idx",
      "type": "integer"
    },
    "page_size": {
      "default": 100,
      "maximum": 500,
      "minimum": 1,
      "title": "Page Size",
      "type": "integer"
    },
    "path_prefix": {
      "default": "/",
      "maxLength": 512,
      "minLength": 1,
      "title": "Path Prefix",
      "type": "string"
    }
  },
  "required": [
    "experiment_id",
    "evidence_id"
  ],
  "title": "GetRequestShapePayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
