# `replay_request`

## Contract

- **Operation:** `replay_request`
- **Action:** `runBrowserExperiment`
- **Purpose:** replay one exact captured ordinary request in browser context with explicit bindings, mutations, response reading, termination, verification, and optional factual comparison.
- **Consequential:** yes; replay may mutate remote state.
- **Prerequisites:** an open aligned session and exact source `experiment_id + evidence_id` for a complete local request snapshot.

## Decoded payload schema

Required fields:

- `session_id`: safe identifier, max 128 characters.
- `objective`: non-empty objective, max 2048 characters.
- `source`: exact `experiment_id` and `evidence_id`.

Optional fields and defaults:

- `mutations`, `extractors`, `bindings`: each max 32; bindings resolve before ordered mutations.
- `target`, `setup_flow` (max 20), `wait_for`, and `verification_flow` (max 20).
- `execution_mode`: `job` or `sync`; default `job`.
- `deadline_ms`: 1000–42000; `job_timeout_ms`: 10000–1800000.
- `query_serialization`: `preserve_raw` by default or `normalize`.
- `transport`, `response_reader`, `termination`, `comparison`, `capture`, `requirements`, `network_evidence`, and `series`.

Constraints: `execution_context` is always `browser_context`; `target.start_url` is forbidden; setup and verification step IDs must be unique; extractor and binding IDs must be unique; browser-managed headers cannot be mutated; `replay_request` is a reserved selector ID.

Decoded example:

```json
{"session_id":"analysis-main","objective":"zero-mutation baseline replay","source":{"experiment_id":"exp_source","evidence_id":"ev_network_source"},"mutations":[],"extractors":[],"bindings":[],"response_reader":{"mode":"auto"},"termination":{"conditions":[{"type":"network_close"}]},"comparison":null,"execution_mode":"sync"}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "replay_request",
  "operation_contract_hash": "sha256:659a7a6590b724450d4a209514ae1c00b818ffeb43a8d7ec5ad69092d3815203",
  "payload_json": "{\"bindings\":[],\"comparison\":null,\"execution_mode\":\"sync\",\"extractors\":[],\"mutations\":[],\"objective\":\"zero-mutation baseline replay\",\"response_reader\":{\"mode\":\"auto\"},\"session_id\":\"analysis-main\",\"source\":{\"evidence_id\":\"ev_network_source\",\"experiment_id\":\"exp_source\"},\"termination\":{\"conditions\":[{\"type\":\"network_close\"}]}}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `experiment_id`, replay network evidence ID in the terminal manifest, mutation/binding observations, optional comparison results, and response artifacts.

Safe retry: poll `get_experiment` for job mode. Never repeat a state-changing replay when dispatch started or outcome is unknown; inspect the experiment, exact replay evidence, and persistent state first.

Typical errors: `invalid_operation_payload`, `source_evidence_not_found`, `source_snapshot_incomplete`, `session_busy`, `browser_busy`, `operation_outcome_unknown`.

Next recommended inspect operations: `get_experiment`, `list_evidence`, and `get_network_evidence` for the exact replay request.

Contract hash: `sha256:659a7a6590b724450d4a209514ae1c00b818ffeb43a8d7ec5ad69092d3815203`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `run`
- Consequential: `true`
- Operation contract hash: `sha256:659a7a6590b724450d4a209514ae1c00b818ffeb43a8d7ec5ad69092d3815203`
<!-- END GENERATED CONTRACT -->
