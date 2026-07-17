# `get_experiment`

## Contract

- **Operation:** `get_experiment`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** return bounded status and summary for one exact experiment.
- **Consequential:** no.
- **Prerequisites:** an exact `experiment_id` from a run response or `list_experiments`.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.

Optional fields: none.

Decoded example:

```json
{"experiment_id":"exp_capture"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "get_experiment",
  "operation_contract_hash": "sha256:21eea9b220bf312f26fc0d04b91696db3c670c9766397d1768e3b95859e43cd3",
  "payload_json": "{\"experiment_id\":\"exp_capture\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: terminal/running status, session ID, quality summary, comparison results, evidence counts, and manifest relative path.

Safe retry: read-only. For job-mode runs, poll this operation until `completed`, `partial`, `failed`, or `interrupted`; do not repeat the original run while status is `running`.

Typical errors: `invalid_operation_payload`, `experiment_not_found`.

Next recommended inspect operation: `list_evidence` after a terminal state.

Contract hash: `sha256:21eea9b220bf312f26fc0d04b91696db3c670c9766397d1768e3b95859e43cd3`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:21eea9b220bf312f26fc0d04b91696db3c670c9766397d1768e3b95859e43cd3`
<!-- END GENERATED CONTRACT -->
