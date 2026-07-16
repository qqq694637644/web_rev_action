# `search_scripts`

## Contract

- **Operation:** `search_scripts`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** search currently loaded browser scripts for bounded source candidates.
- **Consequential:** no.
- **Prerequisites:** an open aligned session; prefer initiator evidence to narrow the query and URL filter.

## Decoded payload schema

Required fields:

- `session_id`: safe identifier, max 128 characters.
- `query`: non-empty text, max 4096 characters.

Optional fields and defaults:

- `url_filter`: optional script URL filter, max 4096 characters.
- `max_results`: 1–100; default 30.
- `exclude_minified`: default false.

Constraints: results are candidates, not proof of request initiation. Keep query and result count narrow.

Decoded example:

```json
{"session_id":"analysis-main","query":"parent_message_id","max_results":20}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "search_scripts",
  "operation_contract_hash": "sha256:2b7ccf6dbfa18f1f2928629dbbaadfcb222efc5feb0740e264ecdc1ee209641a",
  "payload_json": "{\"max_results\":20,\"query\":\"parent_message_id\",\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: bounded matches with script URL/ID, line or offset hints, and result count.

Safe retry: read-only. Refine the query or URL filter rather than expanding blindly. Correlate candidates with initiator evidence.

Typical errors: `invalid_operation_payload`, `session_not_found`, `page_alignment_failed`, `browser_busy`.

Next recommended inspect operation: `get_script_source` for one selected exact script and bounded range.

Contract hash: `sha256:2b7ccf6dbfa18f1f2928629dbbaadfcb222efc5feb0740e264ecdc1ee209641a`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:2b7ccf6dbfa18f1f2928629dbbaadfcb222efc5feb0740e264ecdc1ee209641a`
<!-- END GENERATED CONTRACT -->
