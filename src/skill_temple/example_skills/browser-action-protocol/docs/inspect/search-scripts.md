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
  "operation_contract_hash": "sha256:8fc4808bd71079c56e601f2cd37e4aabb65e6a6120189a86a816a1ca996fb097",
  "payload_json": "{\"max_results\":20,\"query\":\"parent_message_id\",\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:f66c3d13f268880a753a0f46098997becee5f0b3ef299232d63f7fe0ef5f7d24",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: bounded matches with script URL/ID, line or offset hints, and result count.

Safe retry: read-only. Refine the query or URL filter rather than expanding blindly. Correlate candidates with initiator evidence.

Typical errors: `invalid_operation_payload`, `session_not_found`, `page_alignment_failed`, `browser_busy`.

Next recommended inspect operation: `get_script_source` for one selected exact script and bounded range.

Contract hash: `sha256:8fc4808bd71079c56e601f2cd37e4aabb65e6a6120189a86a816a1ca996fb097`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `SearchScriptsRequest`
- Payload model: `SearchScriptsPayload`
- Registry handler: `_inspect_search_scripts`
- Consequential: `false`
- Operation contract hash: `sha256:8fc4808bd71079c56e601f2cd37e4aabb65e6a6120189a86a816a1ca996fb097`

```json
{
  "additionalProperties": false,
  "properties": {
    "exclude_minified": {
      "default": false,
      "title": "Exclude Minified",
      "type": "boolean"
    },
    "max_results": {
      "default": 30,
      "maximum": 100,
      "minimum": 1,
      "title": "Max Results",
      "type": "integer"
    },
    "query": {
      "maxLength": 4096,
      "minLength": 1,
      "title": "Query",
      "type": "string"
    },
    "session_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Session Id",
      "type": "string"
    },
    "url_filter": {
      "anyOf": [
        {
          "maxLength": 4096,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Url Filter"
    }
  },
  "required": [
    "session_id",
    "query"
  ],
  "title": "SearchScriptsPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
