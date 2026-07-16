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

Constraints: line and offset ranges are mutually exclusive; the target experiment must belong to the supplied session; an initiator ID must reference network-request evidence. This operation persists JavaScript text only. A WASM metadata response is rejected rather than serialized into a misleading `.js` artifact; use the pinned js-reverse source-save capability when real `.wasm` bytes are required.

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
  "operation_contract_hash": "sha256:1437910f39f4b14bceb15d9a8dcff9e7b06781f2d5e2e2b5a503183f0574ccd1",
  "payload_json": "{\"length\":4000,\"offset\":0,\"script_id\":\"script-17\",\"session_id\":\"analysis-main\",\"target_experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: script-source `evidence_id`, target `experiment_id`, SHA-256, artifact IDs, and relative artifact paths.

Safe retry: validation failures are safe to correct before dispatch. After dispatch started, inspect `list_evidence` on the target experiment before attempting another save to avoid duplicate evidence.

Typical errors: `invalid_operation_payload`, `script_target_session_mismatch`, `initiator_evidence_kind_invalid`, `wasm_source_not_saved`, `session_not_found`, `operation_outcome_unknown`.

Next recommended inspect operation: `list_evidence` filtered to `script_source`.

Contract hash: `sha256:1437910f39f4b14bceb15d9a8dcff9e7b06781f2d5e2e2b5a503183f0574ccd1`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `run`
- Consequential: `true`
- Operation contract hash: `sha256:1437910f39f4b14bceb15d9a8dcff9e7b06781f2d5e2e2b5a503183f0574ccd1`
<!-- END GENERATED CONTRACT -->
