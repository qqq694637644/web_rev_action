# `capture_flow`

## Contract

- **Operation:** `capture_flow`
- **Action:** `runBrowserExperiment`
- **Purpose:** run one atomic page-flow experiment while capturing bounded browser, network, stream, console, screenshot, and trace evidence.
- **Consequential:** yes; steps may navigate or mutate page/server state.
- **Prerequisites:** an open aligned session; read the exact step and evidence requirements in the workflow Skill before dispatch.

## Decoded payload schema

Required fields:

- `session_id`: safe identifier, max 128 characters.
- `objective`: non-empty experiment objective, max 2048 characters.

Optional fields and defaults:

- `target`: current-page selector; `start_url` is forbidden for capture.
- `primary_request`: request matcher and expected count bounds. `mime_types` is enforced against the normalized response MIME exposed by the pinned js-reverse fork; a request with a missing or different MIME does not match.
- `flow`: up to 100 typed steps.
- `wait_for`: one terminal wait condition.
- `execution_mode`: `job` or `sync`; default `job`.
- `deadline_ms`: 1000–42000; default 42000.
- `job_timeout_ms`: 10000–1800000; default 300000.
- `capture`, `requirements`, `network_evidence` (max 20), and `series`.

Constraints: navigation must be an explicit `navigate` flow step so capture starts first. Step fields are strict and action-specific.

Decoded example:

```json
{"session_id":"analysis-main","objective":"capture current page baseline","primary_request":{"expected_min_matches":0,"expected_max_matches":100},"flow":[{"step_id":"before","action":"snapshot"}],"execution_mode":"sync"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "capture_flow",
  "operation_contract_hash": "sha256:aeebdec176e744912811b9ccddf74643c07af5a0c430dcc4fb6e655228cd0d0b",
  "payload_json": "{\"execution_mode\":\"sync\",\"flow\":[{\"action\":\"snapshot\",\"step_id\":\"before\"}],\"objective\":\"capture current page baseline\",\"primary_request\":{\"expected_max_matches\":100,\"expected_min_matches\":0},\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `experiment_id`, `session_id`, status, bounded experiment summary, and manifest relative path. Public network summaries preserve scheme, host, path, query parameter names, order, and repeats while replacing every query value and removing fragments.

Safe retry: a `running` response is not a failure; poll `get_experiment`. When dispatch started or outcome is unknown, inspect the returned/known experiment and session instead of repeating the flow. A canceled mutation is `canceled` when no adapter dispatch was confirmed and `canceled_outcome_unknown` only when the adapter recorded a sent command. A malformed stream-start response is an unknown consequential outcome; the backend searches the experiment namespace for `capture.json` and uses an exact same-generation handle for cleanup when available.

Typical errors: `invalid_operation_payload`, `session_not_found`, `session_busy`, `browser_busy`, `page_alignment_failed`, `invalid_adapter_response`, `operation_outcome_unknown`.

Next recommended inspect operations: `get_experiment`, then `list_evidence`.

Contract hash: `sha256:aeebdec176e744912811b9ccddf74643c07af5a0c430dcc4fb6e655228cd0d0b`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `run`
- Consequential: `true`
- Operation contract hash: `sha256:aeebdec176e744912811b9ccddf74643c07af5a0c430dcc4fb6e655228cd0d0b`
<!-- END GENERATED CONTRACT -->
