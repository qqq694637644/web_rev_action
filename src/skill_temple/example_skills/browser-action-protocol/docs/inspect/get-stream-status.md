# `get_stream_status`

## Contract

- **Operation:** `get_stream_status`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** return bounded live or persisted stream-collector state for an experiment.
- **Consequential:** no.
- **Prerequisites:** an exact experiment ID; use the capture UUID from prior status or manifest metadata when selecting one generation.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.

Optional fields:

- `capture_uuid`: stable capture-generation selector, max 128 characters.

Constraints: do not send the obsolete numeric capture ID. A supplied UUID must match the experiment capture.

Decoded example:

```json
{"experiment_id":"exp_capture"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "get_stream_status",
  "operation_contract_hash": "sha256:6685b4128e1f0a2d19b2abe1a5e2afef9d48b4ff4c357bb4a328928732140278",
  "payload_json": "{\"experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: source (`live-mcp` or `manifest`), capture UUID/status, request summaries, collector generation, and persisted runtime metadata.

Safe retry: read-only. A transport-generation mismatch falls back to persisted manifest state rather than mutating the collector.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `capture_uuid_mismatch`.

Next recommended inspect operation: `get_experiment` for overall terminal/quality state, then `list_evidence` for persisted stream evidence.

Contract hash: `sha256:6685b4128e1f0a2d19b2abe1a5e2afef9d48b4ff4c357bb4a328928732140278`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:6685b4128e1f0a2d19b2abe1a5e2afef9d48b4ff4c357bb4a328928732140278`
<!-- END GENERATED CONTRACT -->
