# `cancel_experiment`

## Contract

- **Operation:** `cancel_experiment`
- **Action:** `runBrowserExperiment`
- **Purpose:** request cancellation of one known experiment owned by a browser session.
- **Consequential:** yes; it can interrupt active browser work and collector cleanup.
- **Prerequisites:** exact `experiment_id` and owning `session_id`; inspect the experiment first when practical.

## Decoded payload schema

Required fields:

- `experiment_id`: safe identifier, max 128 characters.
- `session_id`: safe identifier, max 128 characters.

Optional fields: none.

Constraints: the experiment must belong to the supplied session. Cancellation does not imply rollback of remote side effects.

Decoded example:

```json
{"experiment_id":"exp_running","session_id":"analysis-main"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "cancel_experiment",
  "operation_contract_hash": "sha256:543ebcf7a9fa084976d4dddfd305a6d6b7cf4d7ea911edb71fe478452eb1f86c",
  "payload_json": "{\"experiment_id\":\"exp_running\",\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `experiment_id`, `session_id`, terminal/current status, manifest path, and collector cleanup state.

Safe retry: after dispatch started, inspect `get_experiment`; do not issue repeated cancellation solely because the client lost the response.

Typical errors: `invalid_operation_payload`, `experiment_not_found`, `experiment_session_mismatch`, `browser_busy`, `operation_outcome_unknown`.

Next recommended inspect operation: `get_experiment`.

Contract hash: `sha256:543ebcf7a9fa084976d4dddfd305a6d6b7cf4d7ea911edb71fe478452eb1f86c`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `run`
- Consequential: `true`
- Operation contract hash: `sha256:543ebcf7a9fa084976d4dddfd305a6d6b7cf4d7ea911edb71fe478452eb1f86c`
<!-- END GENERATED CONTRACT -->
