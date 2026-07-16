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
  "operation_contract_hash": "sha256:2723c7359395284ac7be6e5d57a7a6378a9f72c7f3a688b751c2a8cf8a9bc308",
  "payload_json": "{\"length\":4000,\"offset\":0,\"script_id\":\"script-17\",\"session_id\":\"analysis-main\"}",
  "skill_content_hash": "sha256:786f2331d061583e44fc9dc7344bae933a380d13006b65d1e88f4ae31ad64e6e",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: bounded source text and script/range metadata. This response is transient; use `save_script_source` to persist selected evidence.

Safe retry: read-only. Refine the range when output is too broad; if the script is no longer loaded, recapture the initiator and script handle.

Typical errors: `invalid_operation_payload`, `session_not_found`, `page_alignment_failed`, `browser_busy`, source unavailable.

Next recommended operation: `save_script_source` when the source region supports a claim.

Contract hash: `sha256:2723c7359395284ac7be6e5d57a7a6378a9f72c7f3a688b751c2a8cf8a9bc308`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `GetScriptSourceRequest`
- Payload model: `GetScriptSourcePayload`
- Registry handler: `_inspect_get_script_source`
- Consequential: `false`
- Operation contract hash: `sha256:2723c7359395284ac7be6e5d57a7a6378a9f72c7f3a688b751c2a8cf8a9bc308`

```json
{
  "additionalProperties": false,
  "properties": {
    "end_line": {
      "anyOf": [
        {
          "minimum": 1,
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "End Line"
    },
    "length": {
      "anyOf": [
        {
          "maximum": 200000,
          "minimum": 1,
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Length"
    },
    "offset": {
      "anyOf": [
        {
          "minimum": 0,
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Offset"
    },
    "script_id": {
      "anyOf": [
        {
          "maxLength": 512,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Script Id"
    },
    "session_id": {
      "maxLength": 128,
      "pattern": "^[a-zA-Z0-9_.-]+$",
      "title": "Session Id",
      "type": "string"
    },
    "start_line": {
      "anyOf": [
        {
          "minimum": 1,
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Start Line"
    },
    "url": {
      "anyOf": [
        {
          "maxLength": 8192,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Url"
    }
  },
  "required": [
    "session_id"
  ],
  "title": "GetScriptSourcePayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
