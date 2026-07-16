# `get_script_source`

## Contract

- **Operation:** `get_script_source`
- **Action:** `inspectBrowserEvidence`
- **Purpose:** read one bounded source region from a current browser script.
- **Consequential:** no.
- **Prerequisites:** an open aligned session and an exact script URL or script ID from initiator/search evidence.

## Decoded payload schema

Required fields:

- `session_id`.
- exactly one of `url` or `script_id`.

Optional fields and defaults:

- line range: `start_line` and `end_line` together.
- offset range: `offset` and `length` together; `length` max 200000.

Constraints: line and offset ranges are mutually exclusive. Keep the requested region bounded.

Decoded example:

```json
{"session_id":"analysis-main","script_id":"script-17","offset":0,"length":4000}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "get_script_source",
  "operation_contract_hash": "sha256:aab8f48653050d5584cc62659e38daa0895ac6f4a928e0761fd27e3844861b96",
  "payload_json": "{\"length\":4000,\"offset\":0,\"script_id\":\"script-17\",\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: bounded source text and script/range metadata. This response is transient; use `save_script_source` to persist selected evidence.

Safe retry: read-only. Refine the range when output is too broad; if the script is no longer loaded, recapture the initiator and script handle.

Typical errors: `invalid_operation_payload`, `session_not_found`, `page_alignment_failed`, `browser_busy`, source unavailable.

Next recommended operation: `save_script_source` when the source region supports a claim.

Contract hash: `sha256:aab8f48653050d5584cc62659e38daa0895ac6f4a928e0761fd27e3844861b96`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `inspect`
- Consequential: `false`
- Operation contract hash: `sha256:aab8f48653050d5584cc62659e38daa0895ac6f4a928e0761fd27e3844861b96`
<!-- END GENERATED CONTRACT -->
